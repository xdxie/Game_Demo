from dataclasses import dataclass
from typing import Any

from review_coach.review_coach import ReviewCoach
from review_coach.schemas import ReviewRequest
from review_coach.slow.context_buffer import ContextBuffer
from review_coach.slow.trigger import SlowPriority, channel_for_priority, priority_for_event, should_trigger_slow


@dataclass
class SlowPathResult:
    channel: str
    priority: SlowPriority
    text: str
    review: dict
    context_summary: str
    interrupt_tts: bool = False
    clear_pending_channels: tuple[str, ...] = ()
    expire_sec: float = 8.0


class SlowPath:
    """DESIGN.md SlowPath adapter.

    Converts trigger_slow GameEvents, pattern summaries, and user questions into
    ReviewCoach calls. The returned shape is ready for a TTS queue adapter.
    """

    def __init__(self, coach: ReviewCoach | None = None, context: ContextBuffer | None = None):
        self.coach = coach or ReviewCoach()
        self.context = context or ContextBuffer()
        self.last_fast_text = ""

    def observe_signal(self, timestamp: float, signal: Any) -> None:
        self.context.push_signal(timestamp, signal)

    def observe_action_change(self, timestamp: float, payload: dict[str, Any]) -> None:
        self.context.push_action_change(timestamp, payload)

    def observe_event(self, timestamp: float, event: Any, fast_text: str = "") -> None:
        self.context.push_event(timestamp, event)
        if fast_text:
            self.last_fast_text = fast_text

    def handle(
        self,
        event: Any,
        payload: dict[str, Any] | None = None,
        user_question: str = "",
        last_fast_text: str = "",
    ) -> SlowPathResult | None:
        if not user_question and not should_trigger_slow(event):
            return None

        payload = dict(payload or {})
        priority = priority_for_event(event, user_question)
        context_summary = self.context.summarize()
        query = self._build_query(event, priority, user_question)
        action_context = self._build_action_context(payload, context_summary, last_fast_text or self.last_fast_text)

        payload.update(
            {
                "query": query,
                "action_summary": action_context,
                "trigger_reason": _event_type(event) or payload.get("trigger_reason"),
            }
        )
        payload.setdefault("game_type", "general")
        payload.setdefault("game_name", "")

        request = ReviewRequest.from_payload(payload)
        review = self.coach.generate(request)
        scheduling = _scheduling_for_priority(priority)
        return SlowPathResult(
            channel=channel_for_priority(priority),
            priority=priority,
            text=str(review.get("coaching_text") or ""),
            review=review,
            context_summary=context_summary,
            interrupt_tts=scheduling["interrupt_tts"],
            clear_pending_channels=scheduling["clear_pending_channels"],
            expire_sec=scheduling["expire_sec"],
        )

    @staticmethod
    def _build_query(event: Any, priority: SlowPriority, user_question: str) -> str:
        if user_question:
            return user_question
        event_type = _event_type(event)
        if priority == SlowPriority.SLOW_SUMMARY:
            return "总结刚才这一段操作，给一句最有价值的复盘建议。"
        return f"根据当前局面给一句慢通道建议，触发原因是 {event_type or 'trigger_slow'}。"

    @staticmethod
    def _build_action_context(payload: dict[str, Any], context_summary: str, last_fast_text: str) -> str:
        parts = [context_summary]
        existing = payload.get("action_summary")
        if existing:
            parts.append(f"当前动作摘要：{existing}")
        features = payload.get("action_features")
        if isinstance(features, dict) and features:
            compact_features = ", ".join(f"{key}={value}" for key, value in list(features.items())[:8])
            parts.append(f"动作特征：{compact_features}")
        parts.append(f"刚才快通道已播报：{last_fast_text or '无'}")
        parts.append("要求：不要重复快通道内容，输出适合 TTS 的一句中文。")
        return "\n".join(parts)


def _event_type(event: Any) -> str:
    if isinstance(event, dict):
        raw = event.get("type") or event.get("event_type") or ""
    else:
        raw = getattr(event, "type", "") or getattr(event, "event_type", "")
    if hasattr(raw, "value"):
        return str(raw.value)
    return str(raw)


def _scheduling_for_priority(priority: SlowPriority) -> dict[str, Any]:
    if priority == SlowPriority.USER_ANSWER:
        return {
            "interrupt_tts": True,
            "clear_pending_channels": ("slow", "slow_summary"),
            "expire_sec": 30.0,
        }
    if priority == SlowPriority.SLOW_SUMMARY:
        return {
            "interrupt_tts": False,
            "clear_pending_channels": (),
            "expire_sec": 15.0,
        }
    return {
        "interrupt_tts": False,
        "clear_pending_channels": (),
        "expire_sec": 8.0,
    }
