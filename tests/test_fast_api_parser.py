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


def test_parse_new_schema_wukong_frame():
    data = {
        "left_stick": [0.1757, -0.846],
        "right_stick": [0.0003, -0.0011],
        "buttons_held": [{"name": "RIGHT_SHOULDER", "value": 0.7222}],
        "is_change": True,
        "change_info": {
            "stick_distance": 0.7974,
            "buttons_pressed": ["RIGHT_SHOULDER"],
            "buttons_released": ["RIGHT_TRIGGER"],
        },
    }
    sig = parse_predict_response(data)
    assert sig.move_direction == "BACK"
    assert sig.move_magnitude > 0.7
    assert sig.is_action_change is True
    assert sig.change_distance == 0.7974
    assert any("RIGHT_SHOULDER" in b for b in sig.pressed_buttons)
    assert sig.confidence >= 0.4


def test_parse_includes_buttons_between_025_and_05():
    data = {
        "left_stick": [0.0, 0.0],
        "right_stick": [0.0, 0.0],
        "buttons_held": [{"name": "WEST", "value": 0.35}],
        "is_change": False,
        "change_info": {},
    }
    sig = parse_predict_response(data)
    assert any("WEST(0.35)" == b for b in sig.pressed_buttons)


def test_parse_new_schema_stick_only():
    data = {
        "left_stick": [-0.0655, -0.8969],
        "right_stick": [-0.0001, 0.0001],
        "buttons_held": [],
        "is_change": True,
        "change_info": {"stick_distance": 0.8424},
    }
    sig = parse_predict_response(data)
    assert sig.move_direction == "BACK"
    assert sig.primary_intent == "NAVIGATE"
    assert sig.confidence > 0.12
    assert sig.pressed_buttons == []


def test_parse_raw_v3_shoulder_button():
    data = {
        "j_left": [[0.0, 0.0]] * 18,
        "j_right": [[0.0, 0.0]] * 18,
        "buttons": [[0.0] * 21 for _ in range(18)],
        "button_tokens": [
            "BACK", "DPAD_DOWN", "DPAD_LEFT", "DPAD_RIGHT", "DPAD_UP",
            "EAST", "GUIDE", "LEFT_SHOULDER", "LEFT_THUMB", "LEFT_TRIGGER",
            "NORTH", "RIGHT_BOTTOM", "RIGHT_LEFT", "RIGHT_RIGHT",
            "RIGHT_SHOULDER", "RIGHT_THUMB", "RIGHT_TRIGGER", "RIGHT_UP",
            "SOUTH", "START", "WEST",
        ],
    }
    for i in range(12, 18):
        data["buttons"][i][14] = 1.0
    sig = parse_predict_response(data)
    assert any("RIGHT_SHOULDER" in b for b in sig.pressed_buttons)
