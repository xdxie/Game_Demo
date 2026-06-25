"""
优先级 TTS 队列：同一时间只播一条，USER_ANSWER 可打断其他，
过期内容自动丢弃，与 ASRHandler 联动实现 mute/unmute。
"""

from __future__ import annotations
import heapq
import logging
import threading
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from backend.tts.engine import TTSEngine
    from backend.asr.handler import ASRHandler

logger = logging.getLogger(__name__)


class Priority(IntEnum):
    USER_ANSWER  = 0   # 最高：用户提问的回答
    FAST_HINT    = 1   # 高：快通道关键提示
    SLOW_ADVICE  = 2   # 中：慢通道建议
    SLOW_SUMMARY = 3   # 低：操作段总结


@dataclass(order=True)
class TTSItem:
    priority:     Priority
    enqueue_time: float
    text:         str   = field(compare=False)
    expire_sec:   float = field(compare=False)


# 每类优先级的最大存活时间（秒）
MAX_AGE: dict[Priority, float] = {
    Priority.USER_ANSWER:  30.0,  # 用户回答不丢弃
    Priority.FAST_HINT:     2.0,  # 超过 2 秒的快提示直接丢
    Priority.SLOW_ADVICE:   8.0,
    Priority.SLOW_SUMMARY: 15.0,
}


class TTSQueue:
    """
    优先级播报队列。

    调度规则：
    - 同一时间只播一条，播完后 inter_gap 秒后播下一条
    - USER_ANSWER 到来时立即打断当前播报
    - 超过 expire_sec 的 item 弹出时直接丢弃
    - TTS 播报期间通知 ASRHandler mute，结束后 unmute
    """

    def __init__(
        self,
        tts_engine: "TTSEngine",
        asr_handler: Optional["ASRHandler"] = None,
        inter_gap: float = 0.8,
    ):
        self._tts   = tts_engine
        self._asr   = asr_handler
        self._gap   = inter_gap

        self._heap: list[TTSItem] = []
        self._lock = threading.Lock()

        self._is_speaking   = False
        self._current_item: Optional[TTSItem] = None

        # 全局最小间隔追踪（被动播报）
        self._last_passive_time: float = 0.0

        # WebSocket 广播回调（可选）
        self._on_speak_start: Optional = None   # callable(text, channel)
        self._on_speak_end:   Optional = None   # callable()

    def set_callbacks(self, on_start=None, on_end=None):
        self._on_speak_start = on_start
        self._on_speak_end   = on_end

    # ── 入队 ──────────────────────────────────────────────────────────

    def push(self, text: str, priority: Priority):
        """推入播报任务"""
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

        # USER_ANSWER 立即打断
        if priority == Priority.USER_ANSWER and self._is_speaking:
            self._interrupt()
            threading.Timer(0.05, self._speak_next).start()
        elif not self._is_speaking:
            self._speak_next()

    def clear_by_priority(self, priorities: list[Priority]):
        """清空队列中指定优先级的 item（USER_QUESTION 到来时调用）"""
        with self._lock:
            self._heap = [
                item for item in self._heap
                if item.priority not in priorities
            ]
            heapq.heapify(self._heap)

    def clear_and_stop(self):
        """全部清空并停止当前播报（视频 seek 时调用）"""
        with self._lock:
            self._heap.clear()
        self._interrupt()

    # ── 内部调度 ──────────────────────────────────────────────────────

    def _speak_next(self):
        now = time.time()

        with self._lock:
            # 弹出并丢弃过期 item
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

        self._current_item = item
        self._is_speaking  = True

        # 通知 ASR mute
        if self._asr:
            self._asr.mute()

        # 通知前端 TTS 开始
        channel = _priority_to_channel(item.priority)
        if self._on_speak_start:
            self._on_speak_start(item.text, channel)

        logger.info("TTS speak [%s]: %s", channel, item.text)
        self._tts.speak_async(item.text, on_complete=self._on_complete)

    def _on_complete(self):
        self._is_speaking  = False
        self._current_item = None

        # 通知 ASR unmute（含尾部缓冲）
        if self._asr:
            self._asr.unmute()

        if self._on_speak_end:
            self._on_speak_end()

        # 等 inter_gap 后播下一条
        threading.Timer(self._gap, self._speak_next).start()

    def _interrupt(self):
        self._tts.stop()
        if self._asr:
            self._asr.unmute()
        self._is_speaking  = False
        self._current_item = None


def _priority_to_channel(p: Priority) -> str:
    return {
        Priority.USER_ANSWER: "user_answer",
        Priority.FAST_HINT:   "fast",
        Priority.SLOW_ADVICE: "slow",
        Priority.SLOW_SUMMARY: "slow",
    }.get(p, "slow")
