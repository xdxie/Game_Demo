"""raw_v3 chunk adapter 与 schema (3) 解析测试。"""

import json
from pathlib import Path

from backend.fast.action_filter import ActionFilter
from backend.fast.event import EventType
from backend.fast.priority import FastPriority
from backend.fast.templates import render_fast
from backend.nitrogen.fast_api_parser import parse_predict_response
from backend.nitrogen.raw_chunk_adapter import (
    AdapterState,
    is_raw_v3,
    raw_chunk_to_signal,
)

_SAMPLE_ROOT = Path(
    r"d:\Desktop\f726c9132603b8aa716364d5e4d5abaf(1)\f726c9132603b8aa716364d5e4d5abaf"
)



def test_is_raw_v3():
    assert is_raw_v3({"j_left": [], "buttons": [], "button_tokens": []})
    assert not is_raw_v3({"left_stick": [0, 0]})
    assert not is_raw_v3({"action_summary": {}})


def test_raw_chunk_minimal_pressed():
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
    # WEST column index 20 — set last rows high
    for i in range(12, 18):
        data["buttons"][i][20] = 0.72
    sig = raw_chunk_to_signal(data, btn_threshold=0.25)
    assert any(b.startswith("WEST(") for b in sig.pressed_buttons)
    assert sig.primary_intent in ("ATTACK", "WAIT", "NAVIGATE")


def test_parse_sample_frame_0161():
    path = _SAMPLE_ROOT / "frame_0161.json"
    if not path.is_file():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    assert is_raw_v3(data)
    sig = parse_predict_response(data)
    assert any("RIGHT_SHOULDER" in b for b in sig.pressed_buttons)


def test_gt_14s_ding_from_sample_frames():
    """样例 f139 RT 脉冲 + f143 WEST→RT 跨帧，对应视频 ~14s 给我定。"""
    if not (_SAMPLE_ROOT / "frame_0143.json").is_file():
        return
    state = AdapterState()
    af = ActionFilter(modifier_window_sec=0.8)
    spell_ev = None
    for fi in range(138, 144):
        path = _SAMPLE_ROOT / f"frame_{fi:04d}.json"
        if not path.is_file():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        sig = parse_predict_response(data, state=state)
        ev = af.process(sig, fi * 0.1, global_min_interval=0.0)
        if ev and ev.fast_priority == FastPriority.SPELL:
            text = render_fast(ev, "black_myth_wukong")
            if text == "给我定！":
                spell_ev = ev
    assert spell_ev is not None
    assert spell_ev.combo_keys == frozenset({"RIGHT_TRIGGER", "WEST"})


def test_rt_lt_from_sample_frame_1017():
    if not (_SAMPLE_ROOT / "frame_1017.json").is_file():
        return
    state = AdapterState()
    af = ActionFilter(modifier_window_sec=0.8)
    data = json.loads((_SAMPLE_ROOT / "frame_1017.json").read_text(encoding="utf-8"))
    sig = parse_predict_response(data, state=state)
    ev = af.process(sig, 101.7, global_min_interval=0.0)
    assert ev is not None
    assert render_fast(ev, "black_myth_wukong") == "化身！"


def test_peak_detects_sparse_pulse():
    """2/12 sub-steps at 1.0 → mean 0.17，peak 聚合应检出 RT。"""
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
    # RT index 16: only last 2 substeps in effective region (6:18)
    for i in range(16, 18):
        data["buttons"][i][16] = 1.0
    sig = raw_chunk_to_signal(data, btn_threshold=0.25)
    assert any("RIGHT_TRIGGER" in b for b in sig.pressed_buttons)


def test_fast_api_parser_raw_v3_fixture():
    data = {
        "j_left": [[-0.14, -0.86]] * 18,
        "j_right": [[0.0, 0.0]] * 18,
        "buttons": [[0.0] * 21 for _ in range(18)],
        "button_tokens": [
            "BACK", "DPAD_DOWN", "DPAD_LEFT", "DPAD_RIGHT", "DPAD_UP",
            "EAST", "GUIDE", "LEFT_SHOULDER", "LEFT_THUMB", "LEFT_TRIGGER",
            "NORTH", "RIGHT_BOTTOM", "RIGHT_LEFT", "RIGHT_RIGHT",
            "RIGHT_SHOULDER", "RIGHT_THUMB", "RIGHT_TRIGGER", "RIGHT_UP",
            "SOUTH", "START", "WEST",
        ],
        "frame_idx": 0,
        "session_idx": 0,
        "infer_sec": 0.2,
    }
    for i in range(10, 18):
        data["buttons"][i][14] = 1.0  # RIGHT_SHOULDER
    sig = parse_predict_response(data)
    assert sig.move_magnitude > 0.5
    assert any("RIGHT_SHOULDER" in b for b in sig.pressed_buttons)


if __name__ == "__main__":
    test_is_raw_v3()
    test_raw_chunk_minimal_pressed()
    test_parse_sample_frame_0161()
    test_gt_14s_ding_from_sample_frames()
    test_rt_lt_from_sample_frame_1017()
    test_peak_detects_sparse_pulse()
    test_fast_api_parser_raw_v3_fixture()
    print("ok")
