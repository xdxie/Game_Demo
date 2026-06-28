from abc import ABC, abstractmethod
from typing import Any

from review_coach.schemas import ReviewRequest


class BaseGameSkill(ABC):
    name = "base"
    default_event_type = "GENERAL_REVIEW"

    @abstractmethod
    def build_system_prompt(self) -> str:
        pass

    @abstractmethod
    def build_user_prompt(self, request: ReviewRequest, action_summary: str) -> str:
        pass

    def build_rule_response(self, request: ReviewRequest, action_summary: str) -> dict[str, Any] | None:
        return None

    def postprocess(self, result: dict[str, Any], request: ReviewRequest) -> dict[str, Any]:
        if not isinstance(result, dict):
            result = {}

        default_confidence = 0.8 if result.get("coaching_text") else 0.0
        confidence = self._coerce_confidence(result.get("confidence", default_confidence))
        coaching_text = self._trim_text(str(result.get("coaching_text") or ""), max_length=75, min_length=30)
        problem = self._trim_text(str(result.get("problem") or "未识别到明确问题"), max_length=45, min_length=15)

        should_speak = bool(result.get("should_speak", True))
        if not coaching_text or confidence < 0.45:
            should_speak = False

        return {
            "should_speak": should_speak,
            "game_type": request.game_type,
            "event_type": str(result.get("event_type") or self.default_event_type),
            "problem": problem,
            "coaching_text": coaching_text,
            "confidence": confidence,
        }

    @staticmethod
    def _coerce_confidence(value: Any) -> float:
        try:
            confidence = float(value)
        except (TypeError, ValueError):
            confidence = 0.0
        return max(0.0, min(1.0, confidence))

    @staticmethod
    def _trim_text(text: str, max_length: int, min_length: int) -> str:
        text = text.strip()
        if len(text) <= max_length:
            return text

        punctuation = "。！？!?；;"
        best_index = -1
        for index, char in enumerate(text[:max_length]):
            if char in punctuation and index + 1 >= min_length:
                best_index = index + 1
        if best_index != -1:
            return text[:best_index].strip()
        return text[:max_length].rstrip("，,、；;：:")
