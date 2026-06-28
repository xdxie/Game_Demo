from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ActionSequenceSummary:
    action_summary: str
    action_features: dict[str, Any] = field(default_factory=dict)
    change_info: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "action_summary": self.action_summary,
            "action_features": self.action_features,
            "change_info": self.change_info,
        }


class ActionSequenceSummarizer(Protocol):
    """Extension point for upstream frame-level action aggregation."""

    def summarize(self, source: Any) -> ActionSequenceSummary:
        ...


class BasicActionSequenceSummarizer:
    """Small built-in adapter for already-compressed NitroGen signals.

    This is intentionally conservative. A teammate can replace it with a richer
    frame-level summarizer later without changing ReviewCoach or SlowPath.
    """

    def summarize(self, source: Any) -> ActionSequenceSummary:
        if isinstance(source, dict) and _looks_like_summary(source):
            return ActionSequenceSummary(
                action_summary=str(source.get("action_summary") or ""),
                action_features=dict(source.get("action_features") or {}),
                change_info=dict(source.get("change_info") or {}),
            )

        primary_intent = _get(source, "primary_intent", "")
        confidence = _as_float(_get(source, "confidence", 0.0))
        move_direction = _get(source, "move_direction", None)
        move_magnitude = _as_float(_get(source, "move_magnitude", 0.0))
        horizon = _as_list(_get(source, "horizon_sequence", _get(source, "horizon", [])))

        main_movement = _movement_label(move_direction, move_magnitude)
        risk_tags = _risk_tags(primary_intent, move_direction, horizon)
        action_features = {
            "main_movement": main_movement,
            "dominant_pattern": _dominant_pattern(primary_intent, main_movement, horizon),
            "risk_tags": risk_tags,
            "confidence": confidence,
            "horizon_sequence": horizon,
        }

        summary = _summary_text(
            primary_intent=primary_intent,
            confidence=confidence,
            move_direction=move_direction,
            move_magnitude=move_magnitude,
            horizon=horizon,
            risk_tags=risk_tags,
        )
        change_info = {
            "mode": "basic_action_sequence_summarizer",
            "is_change": bool(primary_intent or horizon),
            "perception": {
                "primary_intent": primary_intent,
                "confidence": confidence,
                "move_direction": move_direction,
                "move_magnitude": move_magnitude,
                "horizon_sequence": horizon,
            },
        }
        return ActionSequenceSummary(summary, action_features, change_info)


def build_slow_payload(
    event: Any,
    action_source: Any,
    *,
    game_type: str = "platformer",
    game_name: str = "New Super Mario Bros.",
    query: str = "",
    image_paths: list[str] | None = None,
    summarizer: ActionSequenceSummarizer | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a ReviewCoach/SlowPath payload without depending on host code."""
    summary = (summarizer or BasicActionSequenceSummarizer()).summarize(action_source)
    payload: dict[str, Any] = {
        "type": _event_type(event),
        "trigger_slow": _get(event, "trigger_slow", False),
        "game_type": game_type,
        "game_name": game_name,
        "query": query,
        "image_paths": list(image_paths or []),
        **summary.to_payload(),
    }
    if extra:
        payload.update(extra)
    return payload


def _looks_like_summary(source: dict[str, Any]) -> bool:
    return any(key in source for key in ("action_summary", "action_features", "change_info"))


def _get(source: Any, name: str, default: Any = None) -> Any:
    if isinstance(source, dict):
        return source.get(name, default)
    return getattr(source, name, default)


def _event_type(event: Any) -> str:
    raw = _get(event, "type", _get(event, "event_type", ""))
    if hasattr(raw, "value"):
        return str(raw.value)
    return str(raw or "")


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _movement_label(direction: Any, magnitude: float) -> str:
    if magnitude < 0.2 or not direction:
        return "idle"
    value = str(direction).lower()
    if "left" in value:
        return "left"
    if "right" in value:
        return "right"
    if "forward" in value or "up" in value:
        return "forward"
    if "back" in value or "down" in value:
        return "back"
    return "mixed"


def _dominant_pattern(primary_intent: Any, movement: str, horizon: list[Any]) -> str:
    intent = str(primary_intent or "unknown").lower()
    if horizon:
        return f"{movement}_{intent}_with_horizon"
    return f"{movement}_{intent}"


def _risk_tags(primary_intent: Any, direction: Any, horizon: list[Any]) -> list[str]:
    joined = " ".join(str(item).lower() for item in horizon)
    intent = str(primary_intent or "").lower()
    tags: list[str] = []
    if "jump" in joined:
        tags.append("jump_timing_possible")
    if "attack" in intent or "attack" in joined:
        tags.append("enemy_timing_risk_possible")
    if "dodge" in intent or "dodge" in joined:
        tags.append("danger_timing_risk_possible")
    if direction:
        tags.append("movement_commitment")
    return tags


def _summary_text(
    primary_intent: Any,
    confidence: float,
    move_direction: Any,
    move_magnitude: float,
    horizon: list[Any],
    risk_tags: list[str],
) -> str:
    horizon_text = " -> ".join(str(item) for item in horizon) if horizon else "none"
    risk_text = ",".join(risk_tags) if risk_tags else "none"
    return (
        "NitroGen perception: "
        f"primary_intent={primary_intent or 'unknown'}; "
        f"confidence={confidence:.2f}; "
        f"move_direction={move_direction or 'none'}; "
        f"move_magnitude={move_magnitude:.2f}; "
        f"horizon={horizon_text}; "
        f"risk_tags={risk_text}"
    )

