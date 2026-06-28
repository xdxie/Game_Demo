"""ActionFilter 法术 combo 与 render_fast SPELL 保障。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.fast.action_filter import ActionFilter
from backend.fast.event import EventType
from backend.fast.priority import FastPriority
from backend.fast.templates import render_fast
from backend.nitrogen.fast_api_parser import parse_predict_response
from backend.nitrogen.raw_chunk_adapter import AdapterState


SAMPLE_DIR = Path(
    r"d:\Desktop\f726c9132603b8aa716364d5e4d5abaf(1)"
    r"\f726c9132603b8aa716364d5e4d5abaf"
)


def _load_frame(frame_dir: Path, idx: int):
    data = json.loads((frame_dir / f"frame_{idx:04d}.json").read_text(encoding="utf-8"))
    return data


def test_render_fast_spell_with_sf6_game_id():
    """SPELL + combo_keys 在 SF6 词表下仍应 lookup 到 Wukong 法术名。"""
    af = ActionFilter(modifier_window_sec=0.8)
    state = AdapterState()
    if not SAMPLE_DIR.is_dir():
        return
    ev = None
    for fi in range(144):
        data = _load_frame(SAMPLE_DIR, fi)
        sig = parse_predict_response(data, state=state)
        ev = af.process(sig, fi * 0.1, global_min_interval=0.0)
    assert ev is not None
    assert ev.fast_priority == FastPriority.SPELL
    text = render_fast(ev, "street_fighter_6")
    assert text == "给我定！"


def test_spell_with_modifier_memory_sparse_sample():
    """稀疏采样（139 RT → 143 WEST）仍应靠修饰键记忆命中法术。"""
    if not SAMPLE_DIR.is_dir():
        return
    state = AdapterState()
    af = ActionFilter(modifier_window_sec=0.8)
    ev = None
    for fi in (139, 143):
        data = _load_frame(SAMPLE_DIR, fi)
        sig = parse_predict_response(data, state=state)
        ev = af.process(sig, fi * 0.1, global_min_interval=0.0)
    assert ev is not None
    assert ev.fast_priority == FastPriority.SPELL
    assert render_fast(ev, "black_myth_wukong") == "给我定！"


def test_f1017_huashen():
    if not SAMPLE_DIR.is_dir():
        return
    state = AdapterState()
    af = ActionFilter(modifier_window_sec=0.8)
    for fi in range(1018):
        data = _load_frame(SAMPLE_DIR, fi)
        sig = parse_predict_response(data, state=state)
        ev = af.process(sig, fi * 0.1, global_min_interval=0.0)
        if fi == 1017 and ev is not None:
            assert ev.fast_priority == FastPriority.SPELL
            assert render_fast(ev, "black_myth_wukong") == "化身！"
            return
    raise AssertionError("f1017 化身 event not found")


if __name__ == "__main__":
    test_render_fast_spell_with_sf6_game_id()
    test_spell_with_modifier_memory_sparse_sample()
    test_f1017_huashen()
    print("all tests passed")
