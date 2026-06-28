"""
测试 backend/fast/action_filter.py
覆盖：5 类事件检测、置信度门控、冷却时间、全局最小间隔、reset()

约定：凡是测"某事件是否被检测到"的用例，传 global_min_interval=0.0，
      排除全局间隔干扰，专注于检测逻辑本身。
      TestGlobalMinInterval 类专门测全局间隔策略。
"""

import pytest
from unittest.mock import patch
from tests.conftest import make_signal, make_event
from backend.fast.action_filter import ActionFilter
from backend.fast.event import EventType


# ── Fixture ───────────────────────────────────────────────────────────

@pytest.fixture
def af():
    """默认参数的 ActionFilter"""
    return ActionFilter(
        confidence_threshold=0.75,
        sustained_danger_sec=2.0,
        cooldowns={
            "sudden_dodge":      3.0,
            "attack_window":     4.0,
            "sustained_danger":  8.0,
            "movement_shift":   10.0,
            "pattern_completed": 5.0,
        },
    )


# ── 便捷封装 ──────────────────────────────────────────────────────────

def p(af, signal, t, interval=0.0, wall_time=None):
    """process() with global_min_interval=0 by default; optional wall_time for冷却/间隔测试。"""
    if wall_time is not None:
        with patch("backend.fast.action_filter.time.time", return_value=wall_time):
            return af.process(signal, t, global_min_interval=interval)
    return af.process(signal, t, global_min_interval=interval)


# ── SUDDEN_DODGE ──────────────────────────────────────────────────────

class TestSuddenDodge:
    def test_triggered_on_dodge_from_wait(self, af):
        p(af, make_signal("WAIT", 0.9), 0.0)
        event = p(af, make_signal("DODGE", 0.9), 1.0)
        assert event is not None
        assert event.type == EventType.SUDDEN_DODGE
        assert event.trigger_fast is True
        assert event.trigger_slow is False

    def test_not_triggered_when_prev_already_dodge(self, af):
        p(af, make_signal("DODGE", 0.9), 0.0)
        event = p(af, make_signal("DODGE", 0.9), 1.0)
        assert event is None or event.type != EventType.SUDDEN_DODGE

    def test_not_triggered_below_confidence(self, af):
        p(af, make_signal("WAIT", 0.9), 0.0)
        event = p(af, make_signal("DODGE", confidence=0.5), 1.0)
        assert event is None or event.type != EventType.SUDDEN_DODGE

    def test_triggered_from_attack_to_dodge(self, af):
        p(af, make_signal("ATTACK", 0.9), 0.0)
        event = p(af, make_signal("DODGE", 0.9), 1.0)
        assert event is not None
        assert event.type == EventType.SUDDEN_DODGE

    def test_event_has_correct_timestamp(self, af):
        p(af, make_signal("WAIT"), 0.0)
        event = p(af, make_signal("DODGE", 0.9), 5.3)
        assert event is not None
        assert event.timestamp == 5.3


# ── ATTACK_WINDOW ─────────────────────────────────────────────────────

class TestAttackWindow:
    def test_triggered_from_dodge_to_attack(self, af):
        p(af, make_signal("DODGE", 0.9), 0.0)
        event = p(af, make_signal("ATTACK", 0.85), 1.0)
        assert event is not None
        assert event.type == EventType.ATTACK_WINDOW
        assert event.trigger_fast is True
        assert event.trigger_slow is True   # 同时触发慢系统

    def test_triggered_from_guard_to_attack(self, af):
        p(af, make_signal("GUARD", 0.9), 0.0)
        event = p(af, make_signal("ATTACK", 0.85), 1.0)
        assert event is not None
        assert event.type == EventType.ATTACK_WINDOW

    def test_not_triggered_from_wait_to_attack(self, af):
        p(af, make_signal("WAIT", 0.9), 0.0)
        event = p(af, make_signal("ATTACK", 0.85), 1.0)
        assert event is None or event.type != EventType.ATTACK_WINDOW

    def test_not_triggered_low_attack_confidence(self, af):
        p(af, make_signal("DODGE", 0.9), 0.0)
        event = p(af, make_signal("ATTACK", 0.4), 1.0)  # confidence 过低
        assert event is None or event.type != EventType.ATTACK_WINDOW


# ── SUSTAINED_DANGER ──────────────────────────────────────────────────

class TestSustainedDanger:
    def test_triggered_after_sustained_sec(self, af):
        """DODGE 持续超过 sustained_danger_sec=2.0 → 触发"""
        p(af, make_signal("DODGE", 0.8), 0.0)
        for t in [0.5, 1.0, 1.5]:
            p(af, make_signal("DODGE", 0.8), t)
        event = p(af, make_signal("DODGE", 0.8), 3.0)
        assert event is not None
        assert event.type == EventType.SUSTAINED_DANGER

    def test_not_triggered_before_sustained_sec(self, af):
        p(af, make_signal("DODGE", 0.8), 0.0)
        event = p(af, make_signal("DODGE", 0.8), 1.0)  # 仅 1s < 2s
        assert event is None or event.type != EventType.SUSTAINED_DANGER

    def test_resets_when_leaving_dodge(self, af):
        p(af, make_signal("DODGE", 0.8), 0.0)
        p(af, make_signal("WAIT",  0.9), 1.5)   # 离开 DODGE
        p(af, make_signal("DODGE", 0.8), 2.0)   # 重新进入，计时重置
        event = p(af, make_signal("DODGE", 0.8), 3.0)  # 仅持续 1s < 2s
        assert event is None or event.type != EventType.SUSTAINED_DANGER


# ── PATTERN_COMPLETED ─────────────────────────────────────────────────

class TestPatternCompleted:
    def test_triggered_from_attack_to_wait(self, af):
        p(af, make_signal("ATTACK", 0.9), 0.0)
        event = p(af, make_signal("WAIT", 0.9), 1.0)
        assert event is not None
        assert event.type == EventType.PATTERN_COMPLETED
        assert event.trigger_fast is False   # 只触发慢系统
        assert event.trigger_slow is True

    def test_triggered_from_dodge_to_navigate(self, af):
        p(af, make_signal("DODGE", 0.9), 0.0)
        event = p(af, make_signal("NAVIGATE", 0.9), 1.0)
        assert event is not None
        assert event.type == EventType.PATTERN_COMPLETED

    def test_not_triggered_wait_to_wait(self, af):
        p(af, make_signal("WAIT", 0.9), 0.0)
        event = p(af, make_signal("WAIT", 0.9), 1.0)
        assert event is None or event.type != EventType.PATTERN_COMPLETED

    def test_not_triggered_on_first_frame(self, af):
        """第一帧无前置信号，不应触发"""
        event = p(af, make_signal("WAIT", 0.9), 0.0)
        assert event is None or event.type != EventType.PATTERN_COMPLETED


# ── MOVEMENT_SHIFT ────────────────────────────────────────────────────

class TestMovementShift:
    def test_triggered_on_direction_change(self, af):
        p(af, make_signal("NAVIGATE", 0.7, direction="LEFT",  magnitude=0.8), 0.0)
        event = p(af, make_signal("NAVIGATE", 0.7, direction="RIGHT", magnitude=0.8), 1.0)
        assert event is not None
        assert event.type == EventType.MOVEMENT_SHIFT

    def test_not_triggered_without_prev_direction(self, af):
        p(af, make_signal("NAVIGATE", 0.7, direction=None,   magnitude=0.8), 0.0)
        event = p(af, make_signal("NAVIGATE", 0.7, direction="RIGHT", magnitude=0.8), 1.0)
        assert event is None or event.type != EventType.MOVEMENT_SHIFT

    def test_not_triggered_small_magnitude(self, af):
        p(af, make_signal("NAVIGATE", 0.7, direction="LEFT",  magnitude=0.3), 0.0)
        event = p(af, make_signal("NAVIGATE", 0.7, direction="RIGHT", magnitude=0.3), 1.0)
        assert event is None or event.type != EventType.MOVEMENT_SHIFT

    def test_not_triggered_same_direction(self, af):
        p(af, make_signal("NAVIGATE", 0.7, direction="LEFT", magnitude=0.8), 0.0)
        event = p(af, make_signal("NAVIGATE", 0.7, direction="LEFT", magnitude=0.8), 1.0)
        assert event is None or event.type != EventType.MOVEMENT_SHIFT


# ── 冷却时间 ──────────────────────────────────────────────────────────

class TestCooldown:
    def test_cooldown_prevents_immediate_retrigger(self, af):
        """冷却期（3s）内同类事件不重复触发"""
        p(af, make_signal("WAIT", 0.9), 0.0)
        event1 = p(af, make_signal("DODGE", 0.9), 1.0)
        assert event1 is not None
        assert event1.type == EventType.SUDDEN_DODGE

        af._prev_signal = make_signal("WAIT", 0.9)
        event2 = p(af, make_signal("DODGE", 0.9), 2.0)   # 距上次只有 1s < 3s
        assert event2 is None or event2.type != EventType.SUDDEN_DODGE

    def test_cooldown_expires_after_duration(self, af):
        """超过冷却时间（3s）后可再次触发"""
        p(af, make_signal("WAIT", 0.9), 0.0, wall_time=1000.0)
        p(af, make_signal("DODGE", 0.9), 1.0, wall_time=1000.0)

        af._prev_signal = make_signal("WAIT", 0.9)
        event = p(af, make_signal("DODGE", 0.9), 5.0, wall_time=1005.0)
        assert event is not None
        assert event.type == EventType.SUDDEN_DODGE

    def test_different_events_have_independent_cooldowns(self, af):
        """不同事件类型各有独立冷却"""
        p(af, make_signal("WAIT", 0.9), 0.0)
        p(af, make_signal("DODGE", 0.9), 1.0)  # SUDDEN_DODGE fires, cooldown 3s

        # DODGE→ATTACK → ATTACK_WINDOW（不同类型，独立冷却）
        af._prev_signal = make_signal("DODGE", 0.9)
        event = p(af, make_signal("ATTACK", 0.85), 2.0)
        assert event is not None
        assert event.type == EventType.ATTACK_WINDOW


# ── 全局最小间隔 ──────────────────────────────────────────────────────

class TestGlobalMinInterval:
    def test_first_event_not_blocked(self, af):
        """_last_any_trigger 初始为 -inf，第一个事件不被阻挡"""
        p(af, make_signal("WAIT", 0.9), 0.0)
        # 使用默认 global_min_interval=2.0
        event = af.process(make_signal("DODGE", 0.9), 1.0, global_min_interval=2.0)
        assert event is not None

    def test_global_interval_blocks_rapid_second_trigger(self, af):
        """全局间隔（5s）内不允许第二次触发"""
        p(af, make_signal("WAIT", 0.9), 0.0)
        event1 = af.process(make_signal("DODGE", 0.9), 1.0, global_min_interval=5.0)
        assert event1 is not None

        af._prev_signal = make_signal("WAIT", 0.9)
        event2 = af.process(make_signal("DODGE", 0.9), 2.0, global_min_interval=5.0)
        assert event2 is None

    def test_global_interval_expires(self, af):
        """全局间隔过后允许触发"""
        p(af, make_signal("WAIT", 0.9), 0.0, wall_time=1000.0)
        with patch("backend.fast.action_filter.time.time", return_value=1000.0):
            af.process(make_signal("DODGE", 0.9), 1.0, global_min_interval=2.0)
        with patch("backend.fast.action_filter.time.time", return_value=1005.0):
            af._prev_signal = make_signal("WAIT", 0.9)
            event = af.process(make_signal("DODGE", 0.9), 5.0, global_min_interval=2.0)
        assert event is not None


# ── reset() ───────────────────────────────────────────────────────────

class TestReset:
    def test_reset_clears_prev_signal(self, af):
        p(af, make_signal("DODGE", 0.9), 0.0)
        assert af._prev_signal is not None
        af.reset()
        assert af._prev_signal is None

    def test_reset_preserves_cooldown(self, af):
        """reset 不清空冷却计时（防止 seek 后刷屏）"""
        p(af, make_signal("WAIT", 0.9), 0.0)
        p(af, make_signal("DODGE", 0.9), 1.0)
        last = af._last_trigger.get(EventType.SUDDEN_DODGE, 0.0)
        af.reset()
        assert af._last_trigger.get(EventType.SUDDEN_DODGE, 0.0) == last

    def test_reset_clears_dodge_start(self, af):
        """reset 后 _current_pattern_type 恢复初始状态"""
        p(af, make_signal("DODGE", 0.9), 0.0)
        af.reset()
        assert af._current_pattern_type == "WAIT"

    def test_no_crash_after_reset(self, af):
        """reset 后继续 process 不应崩溃"""
        p(af, make_signal("DODGE", 0.9), 0.0, wall_time=1000.0)
        af.reset()
        result = p(af, make_signal("DODGE", 0.9), 10.0, wall_time=1005.0)
        assert result is not None

    def test_backward_seek_allows_trigger(self, af):
        """seek 回退后视频时钟小于上次触发时间，不应被冷却错误阻挡"""
        p(af, make_signal("WAIT", 0.9), 0.0, wall_time=1000.0)
        fired = p(af, make_signal("DODGE", 0.9), 95.0, wall_time=1000.0)
        assert fired is not None

        af.reset()
        af._prev_signal = make_signal("WAIT", 0.9)
        with patch("backend.fast.action_filter.time.time", return_value=1005.0):
            after_seek = af.process(make_signal("DODGE", 0.9), 11.0)
        assert after_seek is not None
        assert after_seek.type == EventType.SUDDEN_DODGE
