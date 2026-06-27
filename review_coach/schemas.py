from dataclasses import dataclass
from typing import Any


@dataclass
class ReviewRequest:
    game_type: str
    game_name: str
    query: str
    image_paths: list[str]
    action_summary: Any | None = None
    action_features: dict[str, Any] | None = None
    nitrogen_actions: list[dict[str, Any]] | None = None
    clip_start: float | None = None
    clip_end: float | None = None
    trigger_reason: str | None = None
    frame_idx: int | None = None
    session_idx: int | None = None
    is_change: bool | None = None
    change_info: dict[str, Any] | None = None
    source_image: str | None = None
    client_elapsed_sec: float | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "ReviewRequest":
        image_paths = payload.get("image_paths")
        if not image_paths:
            source_image = payload.get("source_image")
            image_paths = [source_image] if source_image else []

        return cls(
            game_type=str(payload.get("game_type") or "general"),
            game_name=str(payload.get("game_name") or ""),
            query=str(payload.get("query") or ""),
            image_paths=list(image_paths or []),
            action_summary=payload.get("action_summary"),
            action_features=payload.get("action_features"),
            nitrogen_actions=payload.get("nitrogen_actions"),
            clip_start=payload.get("clip_start"),
            clip_end=payload.get("clip_end"),
            trigger_reason=payload.get("trigger_reason"),
            frame_idx=payload.get("frame_idx"),
            session_idx=payload.get("session_idx"),
            is_change=payload.get("is_change"),
            change_info=payload.get("change_info"),
            source_image=payload.get("source_image"),
            client_elapsed_sec=payload.get("client_elapsed_sec"),
        )


@dataclass
class ReviewResult:
    should_speak: bool
    game_type: str
    event_type: str
    problem: str
    coaching_text: str
    confidence: float
