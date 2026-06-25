"""
上下文管理：ContextBuffer、ConversationHistory、FastHistory。
这三个对象构成了慢系统 VLM 调用所需的全部历史信息。
"""

from __future__ import annotations
import logging
from collections import deque
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.fast.event import GameEvent
    from backend.nitrogen.parser import PerceptionSignal

logger = logging.getLogger(__name__)


class ContextBuffer:
    """
    维护近期的感知信号序列，供 VLM 理解"刚才发生了什么"。
    不存原始 chunk，存压缩后的意图序列 + 关键事件列表。
    """

    def __init__(self, window_sec: float = 15.0):
        self.window_sec = window_sec
        self._entries: deque[tuple[float, PerceptionSignal]] = deque()
        self._events:  deque[tuple[float, GameEvent]]        = deque()

    def push_signal(self, t: float, signal: "PerceptionSignal"):
        self._entries.append((t, signal))
        self._evict(t)

    def push_event(self, t: float, event: "GameEvent"):
        self._events.append((t, event))
        # 事件也做老化清理
        while self._events and t - self._events[0][0] > self.window_sec * 2:
            self._events.popleft()

    def _evict(self, now: float):
        while self._entries and now - self._entries[0][0] > self.window_sec:
            self._entries.popleft()

    def summarize(self) -> str:
        """输出供 VLM 使用的上下文描述"""
        if not self._entries:
            return "无近期动作记录"

        # 压缩意图序列（run-length）
        intents = [s.primary_intent for _, s in self._entries]
        compressed = _run_length(intents)

        # 近期关键事件（在时间窗口内的）
        oldest_t = self._entries[0][0] if self._entries else 0.0
        recent_events = [
            f"[{t:.1f}s] {e.type.value}"
            for t, e in self._events
            if t >= oldest_t
        ]

        return (
            f"近{self.window_sec:.0f}秒动作序列：{compressed}\n"
            f"关键事件：{', '.join(recent_events) or '无'}"
        )

    def clear(self):
        self._entries.clear()
        self._events.clear()


class ConversationHistory:
    """
    维护用户与 VLM 的多轮对话历史，专用于 USER_QUESTION 触发的问答。
    事件驱动的慢通道建议不计入此历史，避免干扰后续 VLM 对用户意图的理解。
    """

    MAX_TURNS = 5

    def __init__(self):
        self._turns: list[tuple[str, str]] = []   # [(user_text, ai_response), ...]

    def add_turn(self, user_text: str, ai_response: str):
        self._turns.append((user_text, ai_response))
        if len(self._turns) > self.MAX_TURNS:
            self._turns.pop(0)

    def to_messages(self) -> list[dict]:
        """转换为 Claude API messages 格式（历史轮，不含当前轮）"""
        messages = []
        for user_text, ai_text in self._turns:
            messages.append({"role": "user",      "content": user_text})
            messages.append({"role": "assistant", "content": ai_text})
        return messages

    def clear(self):
        self._turns.clear()

    def __len__(self):
        return len(self._turns)


class FastHistory:
    """
    记录近期快通道已播报的内容，供慢系统 VLM 生成时避免内容重复。
    """

    EXPIRE_SEC = 10.0

    def __init__(self):
        self._records: deque[tuple[float, str]] = deque()  # (video_time, text)

    def record(self, video_time: float, text: str):
        self._records.append((video_time, text))

    def get_recent_summary(self, current_time: float, max_items: int = 3) -> str:
        """返回未过期的近期快通道提示，注入 VLM prompt"""
        recent = [
            text for ts, text in self._records
            if current_time - ts < self.EXPIRE_SEC
        ]
        if not recent:
            return "无"
        return "、".join(recent[-max_items:])

    def clear(self):
        self._records.clear()


# ── 工具函数 ──────────────────────────────────────────────────────────

def _run_length(seq: list[str]) -> str:
    """run-length 压缩，返回可读字符串"""
    if not seq:
        return "（空）"
    result, cur, count = [], seq[0], 1
    for s in seq[1:]:
        if s == cur:
            count += 1
        else:
            result.append(f"{cur}×{count}")
            cur, count = s, 1
    result.append(f"{cur}×{count}")
    return " → ".join(result)
