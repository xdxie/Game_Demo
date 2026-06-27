from enum import IntEnum
from typing import Any


class SlowPriority(IntEnum):
    USER_ANSWER = 0
    SLOW_ADVICE = 2
    SLOW_SUMMARY = 3


def should_trigger_slow(event: Any) -> bool:
    event_type = _event_type(event)
    if event_type == "user_question":
        return True
    if event_type == "pattern_completed":
        return True
    return bool(_get_value(event, "trigger_slow", default=False))


def priority_for_event(event: Any, user_question: str = "") -> SlowPriority:
    event_type = _event_type(event)
    if user_question or event_type == "user_question":
        return SlowPriority.USER_ANSWER
    if event_type == "pattern_completed":
        return SlowPriority.SLOW_SUMMARY
    return SlowPriority.SLOW_ADVICE


def channel_for_priority(priority: SlowPriority) -> str:
    if priority == SlowPriority.USER_ANSWER:
        return "user_answer"
    if priority == SlowPriority.SLOW_SUMMARY:
        return "slow_summary"
    return "slow"


def _event_type(event: Any) -> str:
    raw = _get_value(event, "type", "event_type", default="")
    if hasattr(raw, "value"):
        return str(raw.value)
    return str(raw)


def _get_value(source: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(source, dict) and name in source:
            return source[name]
        if hasattr(source, name):
            return getattr(source, name)
    return default
