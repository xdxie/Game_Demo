from collections import deque
from dataclasses import dataclass
from typing import Any


@dataclass
class ContextEntry:
    timestamp: float
    intent: str
    confidence: float | None = None
    direction: str | None = None
    summary: str = ""


@dataclass
class ContextEvent:
    timestamp: float
    event_type: str


class ContextBuffer:
    """Recent compressed context for SlowPath prompts.

    This stores compact perception/action summaries instead of raw NitroGen chunks.
    """

    def __init__(self, window_sec: float = 15.0):
        self.window_sec = window_sec
        self._entries: deque[ContextEntry] = deque()
        self._events: deque[ContextEvent] = deque()

    def push_signal(self, timestamp: float, signal: Any) -> None:
        entry = ContextEntry(
            timestamp=timestamp,
            intent=_get_value(signal, "primary_intent", "intent", default="UNKNOWN"),
            confidence=_coerce_float(_get_value(signal, "confidence")),
            direction=_get_value(signal, "move_direction", "direction"),
            summary=_signal_summary(signal),
        )
        self._entries.append(entry)
        self._evict(timestamp)

    def push_action_change(self, timestamp: float, payload: dict[str, Any]) -> None:
        action_summary = payload.get("action_summary") if isinstance(payload, dict) else None
        intent = "CHANGE" if payload.get("is_change") else "STEADY"
        entry = ContextEntry(
            timestamp=timestamp,
            intent=intent,
            confidence=None,
            direction=None,
            summary=_action_change_summary(action_summary),
        )
        self._entries.append(entry)
        event_type = _get_event_type(payload)
        if event_type:
            self.push_event(timestamp, event_type)
        self._evict(timestamp)

    def push_event(self, timestamp: float, event: Any) -> None:
        self._events.append(ContextEvent(timestamp=timestamp, event_type=_get_event_type(event) or "unknown"))
        self._evict(timestamp)

    def summarize(self) -> str:
        if not self._entries:
            return "无近期动作记录"

        compressed = _run_length([entry.intent for entry in self._entries])
        details = [entry.summary for entry in list(self._entries)[-4:] if entry.summary]
        start_time = self._entries[0].timestamp
        recent_events = [
            f"[{event.timestamp:.1f}s] {event.event_type}"
            for event in self._events
            if event.timestamp >= start_time
        ]

        lines = [
            f"近{self.window_sec:.0f}秒动作序列：{compressed}",
            f"关键事件：{', '.join(recent_events) or '无'}",
        ]
        if details:
            lines.append("近期动作摘要：" + " | ".join(details))
        return "\n".join(lines)

    def _evict(self, now: float) -> None:
        while self._entries and now - self._entries[0].timestamp > self.window_sec:
            self._entries.popleft()
        while self._events and now - self._events[0].timestamp > self.window_sec:
            self._events.popleft()


def _run_length(items: list[str]) -> str:
    if not items:
        return "无"
    chunks: list[tuple[str, int]] = []
    for item in items:
        if chunks and chunks[-1][0] == item:
            chunks[-1] = (chunks[-1][0], chunks[-1][1] + 1)
        else:
            chunks.append((item, 1))
    return " → ".join(f"{name}x{count}" for name, count in chunks)


def _get_value(source: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(source, dict) and name in source:
            return source[name]
        if hasattr(source, name):
            return getattr(source, name)
    return default


def _get_event_type(event: Any) -> str | None:
    event_type = _get_value(event, "type", "event_type")
    if hasattr(event_type, "value"):
        return str(event_type.value)
    return str(event_type) if event_type else None


def _coerce_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _signal_summary(signal: Any) -> str:
    horizon = _get_value(signal, "horizon_sequence", "horizon")
    if isinstance(horizon, list) and horizon:
        return "horizon=" + "→".join(str(item) for item in horizon[:8])
    return ""


def _action_change_summary(action_summary: Any) -> str:
    if not isinstance(action_summary, dict):
        return ""
    parts: list[str] = []
    left = action_summary.get("left_stick_mean")
    if isinstance(left, list) and len(left) >= 2:
        parts.append(f"left=({float(left[0]):.2f},{float(left[1]):.2f})")
    buttons = action_summary.get("buttons_avg_pressed")
    if isinstance(buttons, list):
        active = [str(button) for button in buttons if str(button) != "(none)"]
        if active:
            parts.append("buttons=" + ",".join(active[:3]))
    return "; ".join(parts)
