"""三游戏快通道 TTS 调优单元测试。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.fast.action_filter import ActionFilter, FORZA_GAME_ID
from backend.fast.event import EventType, GameEvent
from backend.fast.output_gate import FastGateConfig, FastOutputGate
from backend.fast.priority import FastPriority
from backend.fast.templates import render_fast, reset_variant_rotation
from backend.nitrogen.parser import PerceptionSignal


def _btn_event(button_name: str, game_id: str = "new_super_mario_bros") -> GameEvent:
    sig = PerceptionSignal(
        primary_intent="ATTACK", confidence=0.9,
        move_direction=None, move_magnitude=0.0,
        pressed_buttons=[f"{button_name}(0.95)"],
    )
    return GameEvent(
        type=EventType.BUTTON_PRESS,
        timestamp=1.0,
        perception=sig,
        trigger_fast=True,
        trigger_slow=False,
        button_name=button_name,
        fast_priority=FastPriority.BUTTON,
    )


def test_mario_jump_variants_no_qitiao():
    reset_variant_rotation()
    e = _btn_event("SOUTH")
    t1 = render_fast(e, "new_super_mario_bros")
    t2 = render_fast(e, "new_super_mario_bros")
    assert t1 != t2 or t1 in ("顶一下！", "踩一下！", "踩它！")
    assert "起跳" not in t1
    assert "起跳" not in t2
    assert t1 in ("顶一下！", "踩一下！", "踩它！")


def test_wukong_west_silent():
    e = _btn_event("WEST", "black_myth_wukong")
    assert render_fast(e, "black_myth_wukong") == ""


def test_wukong_repeat_text_gate():
    gate = FastOutputGate()
    sig = PerceptionSignal(
        primary_intent="ATTACK", confidence=0.9,
        move_direction=None, move_magnitude=0.0,
        pressed_buttons=["NORTH(0.95)"],
    )
    ev = GameEvent(
        type=EventType.BUTTON_PRESS,
        timestamp=1.0,
        perception=sig,
        trigger_fast=True,
        trigger_slow=False,
        button_name="NORTH",
        fast_priority=FastPriority.BUTTON,
    )
    text = "重击！"
    assert gate.should_speak(ev, text, "black_myth_wukong", now=1000.0)
    assert not gate.should_speak(ev, text, "black_myth_wukong", now=1001.0)
    assert gate.should_speak(ev, text, "black_myth_wukong", now=1003.0)


def test_forza_lt_low_threshold_detect():
    af = ActionFilter()
    prev = PerceptionSignal(
        primary_intent="NAVIGATE", confidence=0.5,
        move_direction="FORWARD", move_magnitude=0.8,
        brake=0, pressed_buttons=[],
    )
    cur = PerceptionSignal(
        primary_intent="DODGE", confidence=0.6,
        move_direction="FORWARD", move_magnitude=0.8,
        brake=0,
        pressed_buttons=["LEFT_TRIGGER(0.18)"],
        is_action_change=True,
    )
    af.process(prev, 0.0, global_min_interval=0.0, game_id=FORZA_GAME_ID)
    ev = af.process(cur, 0.1, global_min_interval=0.0, game_id=FORZA_GAME_ID)
    assert ev is not None
    assert ev.type == EventType.BUTTON_PRESS
    assert ev.button_name == "LEFT_TRIGGER"


def test_forza_brake_shorter_cooldown():
    gate = FastOutputGate(FastGateConfig(forza_brake_cooldown_sec=0.5))
    sig = PerceptionSignal(
        primary_intent="DODGE", confidence=0.8,
        move_direction=None, move_magnitude=0.0,
        pressed_buttons=["LEFT_TRIGGER(0.9)"],
        brake=1,
    )
    ev = GameEvent(
        type=EventType.BUTTON_PRESS,
        timestamp=1.0,
        perception=sig,
        trigger_fast=True,
        trigger_slow=False,
        button_name="LEFT_TRIGGER",
        fast_priority=FastPriority.BUTTON,
    )
    assert gate.should_speak(ev, "刹车！", FORZA_GAME_ID, now=1000.0)
    assert not gate.should_speak(ev, "刹车！", FORZA_GAME_ID, now=1000.3)
    assert gate.should_speak(ev, "刹车！", FORZA_GAME_ID, now=1000.6)


if __name__ == "__main__":
    test_mario_jump_variants_no_qitiao()
    test_wukong_west_silent()
    test_wukong_repeat_text_gate()
    test_forza_lt_low_threshold_detect()
    test_forza_brake_shorter_cooldown()
    print("all tests passed")
