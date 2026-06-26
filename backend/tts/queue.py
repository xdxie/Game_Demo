"""
优先级 TTS 队列：同一时间只播一条，USER_ANSWER 可打断其他，
过期内容自动丢弃，与 ASRHandler 联动实现 mute/unmute。

播放完成：以前端 tts_done 为主，估算时长 + margin 为 fallback。
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
        fallback_margin: float = 1.0,
        broadcast_audio: Optional[Callable[[int, bytes], None]] = None,
    ):
        self._tts   = tts_engine
        self._asr   = asr_handler
        self._gap   = inter_gap
        self._fallback_margin = fallback_margin
        self._broadcast_audio = broadcast_audio

        tts_engine.on_audio_data = self._on_audio_data

        self._heap: list[TTSItem] = []
        self._lock = threading.Lock()

        self._is_speaking   = False
        self._current_item: Optional[TTSItem] = None

        self._utterance_id = 0
        self._pending_done_id: Optional[int] = None
        self._completion_handled = False
        self._fallback_timer: Optional[threading.Timer] = None
        self._speak_token = 0

        self._on_speak_start: Optional[Callable] = None
        self._on_speak_end:   Optional[Callable] = None
        self._on_interrupt:   Optional[Callable] = None

    @property
    def pending_utterance_id(self) -> Optional[int]:
        return self._pending_done_id

    def set_callbacks(self, on_start=None, on_end=None, on_interrupt=None):
        self._on_speak_start = on_start
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
            expire_sec=MAX_AGE[priority],
        )
        with self._lock:
            heapq.heappush(self._heap, item)

        if priority == Priority.USER_ANSWER and self._is_speaking:
            self._interrupt()
            threading.Timer(0.05, self._speak_next).start()
        elif not self._is_speaking:
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
        self._interrupt(notify=notify)

    # ── 内部调度 ──────────────────────────────────────────────────────

    def _on_audio_data(self, mp3_bytes: bytes):
        """TTSEngine 合成完成 → 带 utterance_id 广播"""
        uid = self._pending_done_id
        if uid is None or not self._broadcast_audio:
            return
        try:
            self._broadcast_audio(uid, mp3_bytes)
        except Exception as e:
            logger.error("TTS broadcast_audio error: %s", e)

    def _speak_next(self):
        now = time.time()

        with self._lock:
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

        self._utterance_id += 1
        uid = self._utterance_id
        self._pending_done_id = uid
        self._completion_handled = False
        self._current_item = item
        self._is_speaking  = True

        self._speak_token += 1
        token = self._speak_token

        if self._asr:
            self._asr.mute()

        channel = _priority_to_channel(item.priority)
        if self._on_speak_start:
            self._on_speak_start(item.text, channel, uid)

        logger.info("TTS speak [%s] #%d: %s", channel, uid, item.text)

        def is_cancelled() -> bool:
            return token != self._speak_token

        def _on_dispatched(duration_est: float):
            if is_cancelled():
                return
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
        with self._lock:
            if utterance_id != self._pending_done_id:
                logger.debug("TTS done ignored (stale #%d, pending #%s, src=%s)",
                             utterance_id, self._pending_done_id, source)
                return
            if self._completion_handled:
                return
            self._completion_handled = True

        self._cancel_fallback_timer()
        self._is_speaking  = False
        self._current_item = None
        self._pending_done_id = None

        logger.debug("TTS playback done #%d via %s", utterance_id, source)

        if self._asr:
            self._asr.unmute()

        if self._on_speak_end:
            self._on_speak_end()

        threading.Timer(self._gap, self._speak_next).start()

    def _interrupt(self, notify: bool = True):
        if notify and self._pending_done_id is not None and self._on_interrupt:
            try:
                self._on_interrupt(self._pending_done_id)
            except Exception as e:
                logger.error("TTS on_interrupt callback error: %s", e)

        self._speak_token += 1
        self._cancel_fallback_timer()
        with self._lock:
            self._completion_handled = True
            self._pending_done_id = None

        self._tts.stop()
        self._is_speaking  = False
        self._current_item = None

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
