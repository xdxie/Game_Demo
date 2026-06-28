"""测试 FastOutputGate 优先级分档与方向抑制。"""

import time

import pytest

from backend.fast.event import EventType, GameEvent
from backend.fast.output_gate import FastOutputGate, FastGateConfig
from backend.fast.priority import FastPriority
from backend.fast.templates import render_fast
from backend.tts.queue import Priority
from tests.conftest import make_signal


@pytest.fixture
def gate():
    return FastOutputGate(FastGateConfig(
        p0_cooldown_sec=0.8,
        p1_cooldown_sec=1.2,
        p3_cooldown_sec=4.0,
        wukong_p3_cooldown_sec=25.0,
        directional_suppress_sec=3.0,
        wukong_mag_threshold=0.85,
    ))


def _btn_event(button: str, combo=None, priority=FastPriority.BUTTON):
    return GameEvent(
        type=EventType.BUTTON_PRESS,
        timestamp=0.0,
        perception=make_signal(pressed_buttons=[f"{button}(0.9)"]),
        trigger_fast=True,
        trigger_slow=False,
        button_name=button,
        combo_keys=frozenset(combo) if combo else None,
        fast_priority=priority,
    )


class TestFastOutputGatePriority:
    def test_spell_combo_is_p0(self, gate):
        ev = _btn_event("WEST", combo=["RIGHT_TRIGGER", "WEST"], priority=FastPriority.SPELL)
        assert gate.priority(ev, "给我定！") == FastPriority.SPELL
        assert gate.tts_priority(ev, "给我定！") == Priority.FAST_SPELL

    def test_button_is_p1(self, gate):
        ev = _btn_event("WEST")
        assert gate.priority(ev, "轻攻！") == FastPriority.BUTTON
        assert gate.tts_priority(ev, "轻攻！") == Priority.FAST_HINT


class TestFastOutputGateSpeak:
    def test_empty_text_skipped(self, gate):
        ev = _btn_event("WEST")
        assert gate.should_speak(ev, "", "black_myth_wukong", 100.0) is False

    def test_p0_always_passes_first(self, gate):
        ev = _btn_event("WEST", combo=["RIGHT_TRIGGER", "WEST"], priority=FastPriority.SPELL)
        assert gate.should_speak(ev, "给我定！", "black_myth_wukong", 100.0) is True

    def test_p0_no_cooldown(self, gate):
        ev = _btn_event("WEST", combo=["RIGHT_TRIGGER", "WEST"], priority=FastPriority.SPELL)
        assert gate.should_speak(ev, "给我定！", None, 100.0) is True
        assert gate.should_speak(ev, "广智救我！", None, 100.5) is True

    def test_directional_suppressed_after_button(self, gate):
        btn = _btn_event("WEST")
        assert gate.should_speak(btn, "轻攻！", "black_myth_wukong", 100.0) is True

        shift = GameEvent(
            type=EventType.MOVEMENT_SHIFT,
            timestamp=1.0,
            perception=make_signal("NAVIGATE", direction="RIGHT", magnitude=0.9),
            trigger_fast=True,
            trigger_slow=False,
            fast_priority=FastPriority.DIRECTION,
        )
        text = render_fast(shift, "black_myth_wukong")
        assert gate.should_speak(shift, text, "black_myth_wukong", 101.0) is False

    def test_wukong_low_magnitude_blocked(self, gate):
        shift = GameEvent(
            type=EventType.MOVEMENT_SHIFT,
            timestamp=1.0,
            perception=make_signal("NAVIGATE", direction="RIGHT", magnitude=0.5),
            trigger_fast=True,
            trigger_slow=False,
            fast_priority=FastPriority.DIRECTION,
        )
        text = render_fast(shift, "black_myth_wukong")
        assert gate.should_speak(shift, text, "black_myth_wukong", 200.0) is False

    def test_wukong_high_magnitude_allowed_after_suppress_window(self, gate):
        shift = GameEvent(
            type=EventType.MOVEMENT_SHIFT,
            timestamp=1.0,
            perception=make_signal("NAVIGATE", direction="RIGHT", magnitude=0.9),
            trigger_fast=True,
            trigger_slow=False,
            fast_priority=FastPriority.DIRECTION,
        )
        text = render_fast(shift, "black_myth_wukong")
        assert gate.should_speak(shift, text, "black_myth_wukong", 200.0) is True
