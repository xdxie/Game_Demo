"""action_fast_system /predict JSON 解析"""

import json
from pathlib import Path

from backend.nitrogen.fast_api_parser import (
    format_perception_for_vlm,
    parse_predict_response,
)

_SAMPLES = Path(__file__).resolve().parent.parent / "action_fast_system" / "outputs"


def _load(name: str) -> dict:
    return json.loads((_SAMPLES / name).read_text(encoding="utf-8"))


def test_parse_sample_frame_attack_intent():
    data = _load("frame_0039.json")
    sig = parse_predict_response(data)
    assert sig.primary_intent == "ATTACK"
    assert sig.throttle == 1
    assert "RIGHT_TRIGGER" in sig.hint_text
    assert sig.is_action_change is False


def test_format_perception_avoids_driving_wording():
    data = _load("frame_0039.json")
    sig = parse_predict_response(data)
    text = format_perception_for_vlm(sig)
    assert "油门" not in text
    assert "方向盘" not in text
    assert "手柄" in text
    assert sig.primary_intent in text
