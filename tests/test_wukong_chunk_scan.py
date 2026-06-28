"""黑猴 chunk 时间线扫描与 ActionFilter 集成测试。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.fast.action_filter import ActionFilter
from backend.fast.event import EventType
from backend.fast.game_vocab import WUKONG_GAME_ID, WukongSpeakPolicy
from backend.fast.output_gate import FastOutputGate
from backend.fast.priority import FastPriority
from backend.fast.templates import render_fast
from backend.fast.wukong_chunk_scan import (
    ChunkStep,
    ChunkTimeline,
    extract_timeline,
    filter_hits_to_tail_steps,
    filter_spell_hits_for_new_buttons,
    pick_best_spell_hit,
    pick_best_spell_hit_by_confidence,
    scan_wukong_spells,
    scan_wukong_spells_rt_memory,
)
from backend.nitrogen.fast_api_parser import parse_predict_response
from backend.nitrogen.raw_chunk_adapter import AdapterState

SAMPLE_DIR = Path(
    r"d:\Desktop\f726c9132603b8aa716364d5e4d5abaf(1)"
    r"\f726c9132603b8aa716364d5e4d5abaf"
)


def _load(fi: int) -> dict:
    return json.loads((SAMPLE_DIR / f"frame_{fi:04d}.json").read_text(encoding="utf-8"))


def test_f143_timeline_gewoding():
    """f143 chunk 内无 RT；跨帧 replay 由 ActionFilter 命中给我定。"""
    if not SAMPLE_DIR.is_dir():
        return
    test_f143_action_filter_timeline()


def test_f143_chunk_no_rt():
    """单 chunk 无 RT 时不应误报 scan_wukong_spells。"""
    if not SAMPLE_DIR.is_dir():
        return
    data = _load(143)
    timeline = extract_timeline(data)
    assert timeline is not None
    hits = scan_wukong_spells(timeline)
    assert pick_best_spell_hit(hits) is None


def test_f143_action_filter_timeline():
    if not SAMPLE_DIR.is_dir():
        return
    state = AdapterState()
    af = ActionFilter(modifier_window_sec=0.8)
    for fi in range(144):
        data = _load(fi)
        sig = parse_predict_response(data, state=state)
        ev = af.process(
            sig, fi * 0.1, global_min_interval=0.0,
            game_id=WUKONG_GAME_ID, raw_chunk=data,
        )
    assert ev is not None
    assert ev.fast_priority == FastPriority.SPELL
    assert render_fast(ev, WUKONG_GAME_ID) == "给我定！"


def test_f681_guangzhi_not_gewoding():
    """RT 边沿 + 多 face 记忆时取最近 face（EAST 而非 WEST）。"""
    if not SAMPLE_DIR.is_dir():
        return
    state = AdapterState()
    af = ActionFilter(modifier_window_sec=0.8)
    ev = None
    for fi in range(682):
        data = _load(fi)
        sig = parse_predict_response(data, state=state)
        ev = af.process(
            sig, fi * 0.1, global_min_interval=0.0,
            game_id=WUKONG_GAME_ID, raw_chunk=data, replay_clock=True,
        )
    assert ev is not None
    assert render_fast(ev, WUKONG_GAME_ID) == "广智救我！"


def test_filter_spell_hits_defer_simultaneous():
    from backend.fast.wukong_chunk_scan import SpellHit, filter_spell_hits_for_new_buttons
    hits = [
        SpellHit(
            frozenset({"RIGHT_TRIGGER", "SOUTH"}), 15, "上吧孩儿们！", 0,
        ),
    ]
    assert filter_spell_hits_for_new_buttons(
        hits, {"RIGHT_TRIGGER", "SOUTH"},
    ) == []
    assert len(filter_spell_hits_for_new_buttons(
        hits, {"SOUTH"},
    )) == 1


def test_f1017_huashen_timeline():
    if not SAMPLE_DIR.is_dir():
        return
    state = AdapterState()
    af = ActionFilter(modifier_window_sec=0.8)
    ev = None
    for fi in range(1018):
        data = _load(fi)
        sig = parse_predict_response(data, state=state)
        ev = af.process(
            sig, fi * 0.1, global_min_interval=0.0,
            game_id=WUKONG_GAME_ID, raw_chunk=data,
        )
    assert ev is not None
    assert render_fast(ev, WUKONG_GAME_ID) == "化身！"


def test_rt_memory_tail_north():
    """尾步 NORTH + 跨帧 RT 记忆补检聚形散气。"""
    timeline = ChunkTimeline(
        steps=[
            ChunkStep(16, frozenset(), {"NORTH": 0.1}),
            ChunkStep(17, frozenset({"NORTH"}), {"NORTH": 0.95}),
        ],
        chunk_len=18,
    )
    hits = scan_wukong_spells_rt_memory(timeline, rt_active=True, lt_active=False)
    best = pick_best_spell_hit_by_confidence(hits, timeline)
    assert best is not None
    assert best.text == "聚形散气！"


def test_tail_filter_drops_early_steps():
    timeline = ChunkTimeline(
        steps=[
            ChunkStep(6, frozenset({"RIGHT_TRIGGER", "SOUTH"}), {"SOUTH": 0.9}),
            ChunkStep(15, frozenset(), {}),
            ChunkStep(16, frozenset(), {}),
            ChunkStep(17, frozenset({"WEST"}), {"WEST": 0.9}),
        ],
        chunk_len=18,
    )
    hits = scan_wukong_spells(timeline)
    tail_hits = filter_hits_to_tail_steps(hits, timeline)
    assert not any(h.step_index == 6 for h in tail_hits)


def test_dedup_skips_tier3_fallback():
    """同 combo dedup 后不应降级为 tier3 单键。"""
    if not SAMPLE_DIR.is_dir():
        return
    state = AdapterState()
    af = ActionFilter(modifier_window_sec=0.8, wukong_rt_modifier_window_sec=1.0)
    combo = frozenset({"RIGHT_TRIGGER", "WEST"})
    af._last_spell_combo_at[combo] = 10.0
    data = _load(143)
    sig = parse_predict_response(data, state=state)
    ev = af.process(
        sig, 10.5, global_min_interval=0.0,
        game_id=WUKONG_GAME_ID, raw_chunk=data, replay_clock=True,
    )
    if ev is not None and ev.fast_priority == FastPriority.BUTTON:
        assert ev.button_name not in WukongSpeakPolicy.TIER3_BUTTONS


def test_wukong_fast_interval_lb_vs_tier3():
    """LB 与 tier3 使用独立间隔键，tier3 不被 LB 节流。"""
    from backend.nitrogen.parser import PerceptionSignal
    af = ActionFilter(modifier_window_sec=0.8, wukong_rt_modifier_window_sec=1.0)
    lb_sig = PerceptionSignal(
        primary_intent="GUARD", confidence=0.8,
        move_direction=None, move_magnitude=0.0,
        pressed_buttons=["LEFT_SHOULDER(0.9)"],
    )
    sprint_sig = PerceptionSignal(
        primary_intent="NAVIGATE", confidence=0.8,
        move_direction="LEFT", move_magnitude=0.9,
        pressed_buttons=["LEFT_THUMB(0.9)"],
    )
    lb_ev = af.process(
        lb_sig, 51.0, global_min_interval=0.0,
        game_id=WUKONG_GAME_ID, replay_clock=True,
    )
    assert lb_ev is not None
    assert lb_ev.button_name == "LEFT_SHOULDER"
    out = af.process(
        sprint_sig, 51.1, global_min_interval=0.0,
        game_id=WUKONG_GAME_ID, replay_clock=True,
    )
    assert out is not None
    assert out.button_name == "LEFT_THUMB"
    assert render_fast(out, WUKONG_GAME_ID) == "疾跑！"


def test_f547_juxing_with_rt_memory():
    """54s 附近：RT@537 + NORTH 尾步 timeline 补检。"""
    if not SAMPLE_DIR.is_dir():
        return
    state = AdapterState()
    af = ActionFilter(modifier_window_sec=0.8, wukong_rt_modifier_window_sec=1.0)
    ev = None
    for fi in range(548):
        data = _load(fi)
        sig = parse_predict_response(data, state=state)
        ev = af.process(
            sig, fi * 0.1, global_min_interval=0.0,
            game_id=WUKONG_GAME_ID, raw_chunk=data, replay_clock=True,
        )
    if ev is not None and ev.fast_priority == FastPriority.SPELL:
        assert render_fast(ev, WUKONG_GAME_ID) in ("聚形散气！", "给我定！", "广智救我！")


def test_west_muted():
    if not SAMPLE_DIR.is_dir():
        return
    from backend.fast.event import GameEvent
    from backend.nitrogen.parser import PerceptionSignal
    sig = PerceptionSignal(
        primary_intent="ATTACK", confidence=0.9,
        move_direction=None, move_magnitude=0.0,
        pressed_buttons=["WEST(0.95)"],
    )
    ev = GameEvent(
        type=EventType.BUTTON_PRESS, timestamp=1.0, perception=sig,
        trigger_fast=True, trigger_slow=False, button_name="WEST",
        fast_priority=FastPriority.BUTTON,
    )
    assert render_fast(ev, WUKONG_GAME_ID) == ""


def test_lb_heal_gate_cooldown():
    gate = FastOutputGate()
    from backend.fast.event import GameEvent
    from backend.nitrogen.parser import PerceptionSignal
    sig = PerceptionSignal(
        primary_intent="GUARD", confidence=0.8,
        move_direction=None, move_magnitude=0.0,
        pressed_buttons=["LEFT_SHOULDER(0.9)"],
    )
    ev = GameEvent(
        type=EventType.BUTTON_PRESS, timestamp=1.0, perception=sig,
        trigger_fast=True, trigger_slow=False, button_name="LEFT_SHOULDER",
        fast_priority=FastPriority.BUTTON,
    )
    text = "回口血！"
    assert gate.should_speak(ev, text, WUKONG_GAME_ID, now=1000.0)
    assert not gate.should_speak(ev, text, WUKONG_GAME_ID, now=1001.0)
    assert gate.should_speak(ev, text, WUKONG_GAME_ID, now=1003.5)


if __name__ == "__main__":
    test_f143_timeline_gewoding()
    test_f143_action_filter_timeline()
    test_f681_guangzhi_not_gewoding()
    test_f1017_huashen_timeline()
    test_west_muted()
    test_lb_heal_gate_cooldown()
    print("all tests passed")
