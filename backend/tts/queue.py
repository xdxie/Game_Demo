"""
优先级 TTS 队列：同一时间只播一条，USER_ANSWER 可打断其他，
过期内容自动丢弃，与 ASRHandler 联动实现 mute/unmute。

时序：先 edge-tts 合成 → MP3 就绪后再 mute ASR 并下发播放，
避免「字幕已出但语音迟迟不来」期间误触发 barge-in 打断。
"""

from __future__ import annotations
import heapq
import logging
import threading
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:
    from backend.tts.engine import TTSEngine
    from backend.asr.handler import ASRHandler

logger = logging.getLogger(__name__)


class Priority(IntEnum):
    USER_ANSWER  = 0
    FAST_HINT    = 1
    SLOW_ADVICE  = 2
    SLOW_SUMMARY = 3


@dataclass(order=True)
class TTSItem:
    priority:     Priority
    enqueue_time: float
    text:         str   = field(compare=False)
    expire_sec:   float = field(compare=False)


MAX_AGE: dict[Priority, float] = {
    Priority.USER_ANSWER:  30.0,
    Priority.FAST_HINT:     2.0,
    Priority.SLOW_ADVICE:   8.0,
    Priority.SLOW_SUMMARY: 15.0,
}


class TTSQueue:
    """
    优先级播报队列。
    utterance_id 关联 JSON 字幕与 MP3 二进制，支持前端精确 tts_done 回传。
    speak_token 在打断时递增，使进行中的合成线程结果失效。
    """

    def __init__(
        self,
        tts_engine: "TTSEngine",
        asr_handler: Optional["ASRHandler"] = None,
        inter_gap: float = 0.8,
        user_inter_gap: float = 0.15,
        fallback_margin: float = 1.0,
        broadcast_audio: Optional[Callable[[int, bytes], None]] = None,
        max_age: Optional[dict[Priority, float]] = None,
    ):
        self._tts   = tts_engine
        self._asr   = asr_handler
        self._gap   = inter_gap
        self._user_gap = user_inter_gap
        self._fallback_margin = fallback_margin
        self._broadcast_audio = broadcast_audio
        self._max_age = dict(max_age) if max_age is not None else dict(MAX_AGE)

        tts_engine.on_audio_data = self._on_audio_data

        self._heap: list[TTSItem] = []
        self._lock = threading.Lock()

        self._is_speaking   = False   # 占用播报槽（合成中或播放中）
        self._playback_active = False  # MP3 已下发、正在播放（barge-in 仅此时生效）
        self._current_item: Optional[TTSItem] = None

        self._utterance_id = 0
        self._pending_done_id: Optional[int] = None
        self._completion_handled = False
        self._fallback_timer: Optional[threading.Timer] = None
        self._speak_token = 0

        self._on_speak_start: Optional[Callable] = None
        self._on_playback_start: Optional[Callable] = None
        self._on_speak_end:   Optional[Callable] = None
        self._on_interrupt:   Optional[Callable] = None

    @property
    def pending_utterance_id(self) -> Optional[int]:
        return self._pending_done_id

    @property
    def is_speaking(self) -> bool:
        """是否正在实际播放语音（不含 edge-tts 合成等待阶段）。"""
        with self._lock:
            return self._playback_active

    def set_callbacks(
        self,
        on_start=None,
        on_playback=None,
        on_end=None,
        on_interrupt=None,
    ):
        self._on_speak_start = on_start
        self._on_playback_start = on_playback
        self._on_speak_end   = on_end
        self._on_interrupt   = on_interrupt

    # ── 入队 ──────────────────────────────────────────────────────────

    def push(self, text: str, priority: Priority):
        if not text:
            return
        item = TTSItem(
            priority=priority,
            enqueue_time=time.time(),
            text=text,
            expire_sec=self._max_age[priority],
        )
        interrupt = False
        with self._lock:
            heapq.heappush(self._heap, item)
            if priority == Priority.USER_ANSWER and self._is_speaking:
                interrupt = True

        if interrupt:
            self._interrupt()
        self._speak_next()

    def on_client_tts_done(self, utterance_id: int):
        """前端 Audio.onended 回传，精确完成信号"""
        self._on_playback_done(utterance_id, source="client")

    def clear_by_priority(self, priorities: list[Priority]):
        with self._lock:
            self._heap = [
                item for item in self._heap
                if item.priority not in priorities
            ]
            heapq.heapify(self._heap)

    def clear_and_stop(self, notify: bool = True):
        with self._lock:
            self._heap.clear()
        self._interrupt(notify=notify, unmute_if_idle=True)

    def barge_in_interrupt(self):
        """用户说话打断当前播报，清空被动队列并立即恢复收音。"""
        with self._lock:
            self._heap = [
                item for item in self._heap
                if item.priority == Priority.USER_ANSWER
            ]
            heapq.heapify(self._heap)
        self._interrupt(notify=True, unmute_if_idle=True)
        if self._asr:
            self._asr.force_unmute()
        threading.Timer(0.05, self._speak_next).start()

    # ── 内部调度 ──────────────────────────────────────────────────────

    def _on_audio_data(self, mp3_bytes: bytes):
        """TTSEngine 合成完成 → mute + 带 utterance_id 广播 MP3"""
        with self._lock:
            uid = self._pending_done_id
            if uid is None or self._completion_handled:
                return
            channel = _priority_to_channel(
                self._current_item.priority if self._current_item else Priority.SLOW_ADVICE,
            )
            self._playback_active = True

        if self._asr:
            self._asr.mute()

        if self._on_playback_start:
            try:
                self._on_playback_start(uid, channel)
            except Exception as e:
                logger.error("TTS on_playback_start error: %s", e)

        if not self._broadcast_audio:
            return
        try:
            self._broadcast_audio(uid, mp3_bytes)
        except Exception as e:
            logger.error("TTS broadcast_audio error: %s", e)

    def _speak_next(self):
        now = time.time()

        with self._lock:
            if self._is_speaking:
                return
            item = None
            while self._heap:
                candidate = heapq.heappop(self._heap)
                age = now - candidate.enqueue_time
                if age <= candidate.expire_sec:
                    item = candidate
                    break
                else:
                    logger.debug("TTS item expired: %s [%.1fs old]",
                                 candidate.text[:20], age)
            if item is None:
                return
            self._is_speaking = True
            self._playback_active = False
            self._utterance_id += 1
            uid = self._utterance_id
            self._pending_done_id = uid
            self._completion_handled = False
            self._current_item = item
            self._speak_token += 1
            token = self._speak_token
            synth_started = time.time()

        channel = _priority_to_channel(item.priority)
        if self._on_speak_start:
            self._on_speak_start(item.text, channel, uid)

        logger.info("TTS synth [%s] #%d: %s", channel, uid, item.text)

        def is_cancelled() -> bool:
            return token != self._speak_token

        def _on_dispatched(duration_est: float):
            if is_cancelled():
                return
            elapsed = time.time() - synth_started
            logger.info(
                "TTS ready [%s] #%d in %.2fs (est play %.2fs)",
                channel, uid, elapsed, duration_est,
            )
            self._schedule_fallback(uid, duration_est)

        def _on_error():
            if is_cancelled():
                return
            self._on_playback_done(uid, source="error")

        self._tts.speak_async(
            item.text,
            is_cancelled=is_cancelled,
            on_dispatched=_on_dispatched,
            on_error=_on_error,
        )

    def _schedule_fallback(self, utterance_id: int, duration_est: float):
        self._cancel_fallback_timer()
        delay = duration_est + self._fallback_margin
        self._fallback_timer = threading.Timer(
            delay,
            lambda: self._on_playback_done(utterance_id, source="fallback"),
        )
        self._fallback_timer.start()

    def _on_playback_done(self, utterance_id: int, source: str = "client"):
        finished_priority: Optional[Priority] = None
        with self._lock:
            if utterance_id != self._pending_done_id:
                logger.debug("TTS done ignored (stale #%d, pending #%s, src=%s)",
                             utterance_id, self._pending_done_id, source)
                return
            if self._completion_handled:
                return
            self._completion_handled = True
            if self._current_item is not None:
                finished_priority = self._current_item.priority

        self._cancel_fallback_timer()
        with self._lock:
            self._is_speaking  = False
            self._playback_active = False
            self._current_item = None
            self._pending_done_id = None

        logger.debug("TTS playback done #%d via %s", utterance_id, source)

        if self._asr:
            self._asr.unmute()

        if self._on_speak_end:
            self._on_speak_end()

        gap = (
            self._user_gap
            if finished_priority == Priority.USER_ANSWER
            else self._gap
        )
        threading.Timer(gap, self._speak_next).start()

    def _interrupt(self, notify: bool = True, unmute_if_idle: bool = False):
        with self._lock:
            interrupted_id = self._pending_done_id
            was_playback = self._playback_active
        if notify and interrupted_id is not None and self._on_interrupt:
            try:
                self._on_interrupt(interrupted_id)
            except Exception as e:
                logger.error("TTS on_interrupt callback error: %s", e)

        self._speak_token += 1
        self._cancel_fallback_timer()
        with self._lock:
            self._completion_handled = True
            self._pending_done_id = None

        self._tts.stop()
        with self._lock:
            self._is_speaking  = False
            self._playback_active = False
            self._current_item = None
            still_has_items = len(self._heap) > 0

        if self._asr and unmute_if_idle and not still_has_items:
            self._asr.force_unmute()
        elif self._asr and was_playback:
            self._asr.force_unmute()

    def _cancel_fallback_timer(self):
        if self._fallback_timer is not None:
            self._fallback_timer.cancel()
            self._fallback_timer = None


def _priority_to_channel(p: Priority) -> str:
    return {
        Priority.USER_ANSWER:  "user_answer",
        Priority.FAST_HINT:    "fast",
        Priority.SLOW_ADVICE:  "slow",
        Priority.SLOW_SUMMARY: "slow",
    }.get(p, "slow")
