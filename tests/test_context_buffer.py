"""
测试 backend/fast/templates.py 和 backend/slow/context_buffer.py
"""

import time
import pytest

from tests.conftest import make_signal, make_event
from backend.fast.templates import render_fast, DIRECTION_ZH
from backend.fast.event import EventType, GameEvent
from backend.slow.context_buffer import ContextBuffer, ConversationHistory, FastHistory


# ═══════════════════════════════════════════════════════════════════════
# 快通道模板引擎
# ═══════════════════════════════════════════════════════════════════════

class TestRenderFast:
    def test_sudden_dodge_with_direction(self):
        event = make_event(EventType.SUDDEN_DODGE, signal=make_signal("DODGE", 0.9, "LEFT"))
        text = render_fast(event)
        assert "左" in text     # DIRECTION_ZH["LEFT"] = "向左"
        assert len(text) <= 10  # 快通道文本应简短

    def test_sudden_dodge_without_direction(self):
        event = make_event(EventType.SUDDEN_DODGE, signal=make_signal("DODGE", 0.9, None))
        text = render_fast(event)
        assert text            # 非空
        assert "左" not in text and "右" not in text and "前" not in text

    def test_attack_window_returns_text(self):
        event = make_event(EventType.ATTACK_WINDOW, signal=make_signal("ATTACK", 0.9))
        text = render_fast(event)
        assert text
        assert len(text) <= 10

    def test_sustained_danger_with_direction(self):
        event = make_event(EventType.SUSTAINED_DANGER,
                           signal=make_signal("DODGE", 0.7, "RIGHT"))
        text = render_fast(event)
        assert text

    def test_sustained_danger_without_direction(self):
        event = make_event(EventType.SUSTAINED_DANGER,
                           signal=make_signal("DODGE", 0.7, None))
        text = render_fast(event)
        assert text

    def test_movement_shift_with_direction(self):
        event = make_event(EventType.MOVEMENT_SHIFT,
                           signal=make_signal("NAVIGATE", 0.7, "FORWARD"))
        text = render_fast(event)
        assert text

    def test_unknown_event_fallback(self):
        """不在 FAST_TEMPLATES 中的事件类型 → 返回 fallback 文本"""
        event = make_event(EventType.PATTERN_COMPLETED, fast=True)
        text = render_fast(event)
        assert text   # 不崩溃，返回非空字符串

    def test_direction_mapping_completeness(self):
        """所有方向字符串都有映射，包括 None"""
        for key in ["LEFT", "RIGHT", "FORWARD", "BACK", None]:
            assert key in DIRECTION_ZH


# ═══════════════════════════════════════════════════════════════════════
# ContextBuffer
# ═══════════════════════════════════════════════════════════════════════

class TestContextBuffer:
    def test_push_signal_stores_entries(self):
        buf = ContextBuffer(window_sec=10.0)
        buf.push_signal(1.0, make_signal("DODGE", 0.9))
        buf.push_signal(2.0, make_signal("ATTACK", 0.8))
        assert len(buf._entries) == 2

    def test_eviction_removes_old_entries(self):
        buf = ContextBuffer(window_sec=5.0)
        buf.push_signal(0.0, make_signal("DODGE"))
        buf.push_signal(3.0, make_signal("ATTACK"))
        buf.push_signal(6.0, make_signal("WAIT"))   # 触发 evict：0.0 应被移除
        assert len(buf._entries) == 2
        assert buf._entries[0][0] == 3.0

    def test_summarize_empty(self):
        buf = ContextBuffer()
        text = buf.summarize()
        assert "无" in text or len(text) > 0   # 非空字符串

    def test_summarize_contains_intent(self):
        buf = ContextBuffer(window_sec=30.0)
        buf.push_signal(1.0, make_signal("DODGE"))
        buf.push_signal(2.0, make_signal("ATTACK"))
        text = buf.summarize()
        assert "DODGE" in text or "ATTACK" in text

    def test_push_event_appears_in_summary(self):
        buf = ContextBuffer(window_sec=30.0)
        buf.push_signal(1.0, make_signal("DODGE"))
        buf.push_event(1.0, make_event(EventType.SUDDEN_DODGE, timestamp=1.0))
        text = buf.summarize()
        assert "sudden_dodge" in text

    def test_clear_empties_all(self):
        buf = ContextBuffer()
        buf.push_signal(1.0, make_signal("DODGE"))
        buf.push_event(1.0, make_event())
        buf.clear()
        assert len(buf._entries) == 0
        assert len(buf._events) == 0

    def test_window_sec_is_respected(self):
        """在时间窗口内的信号不应被驱逐"""
        buf = ContextBuffer(window_sec=15.0)
        for t in range(10):
            buf.push_signal(float(t), make_signal("WAIT"))
        buf.push_signal(20.0, make_signal("WAIT"))   # 触发驱逐
        # t=0..4 被驱逐（20-15=5 以前），t=5..9,20 留下
        assert all(ts >= 5.0 for ts, _ in buf._entries)


# ═══════════════════════════════════════════════════════════════════════
# ConversationHistory
# ═══════════════════════════════════════════════════════════════════════

class TestConversationHistory:
    def test_add_turn_stored(self):
        hist = ConversationHistory()
        hist.add_turn("你好", "你好！")
        assert len(hist) == 1

    def test_to_messages_format(self):
        hist = ConversationHistory()
        hist.add_turn("怎么打？", "等右拳落地再打")
        msgs = hist.to_messages()
        assert len(msgs) == 2
        assert msgs[0]["role"] == "user"
        assert msgs[0]["content"] == "怎么打？"
        assert msgs[1]["role"] == "assistant"
        assert msgs[1]["content"] == "等右拳落地再打"

    def test_max_turns_rolling(self):
        hist = ConversationHistory()
        for i in range(7):
            hist.add_turn(f"问{i}", f"答{i}")
        assert len(hist) == ConversationHistory.MAX_TURNS
        # 最旧的应该被滚动丢弃，最新的保留
        msgs = hist.to_messages()
        assert "问6" in [m["content"] for m in msgs]
        assert "问0" not in [m["content"] for m in msgs]

    def test_to_messages_multi_turn_order(self):
        hist = ConversationHistory()
        hist.add_turn("第一问", "第一答")
        hist.add_turn("第二问", "第二答")
        msgs = hist.to_messages()
        assert msgs[0]["content"] == "第一问"
        assert msgs[2]["content"] == "第二问"

    def test_clear(self):
        hist = ConversationHistory()
        hist.add_turn("x", "y")
        hist.clear()
        assert len(hist) == 0
        assert hist.to_messages() == []


# ═══════════════════════════════════════════════════════════════════════
# FastHistory
# ═══════════════════════════════════════════════════════════════════════

class TestFastHistory:
    def test_record_and_get(self):
        fh = FastHistory()
        fh.record(1.0, "向左闪！")
        summary = fh.get_recent_summary(1.5)
        assert "向左闪！" in summary

    def test_expiry_filters_old(self):
        fh = FastHistory()
        fh.record(0.0, "旧提示")
        # EXPIRE_SEC = 10.0，current_time - ts = 15 > 10 → 过期
        summary = fh.get_recent_summary(15.0)
        assert summary == "无"

    def test_max_items_limit(self):
        fh = FastHistory()
        for i in range(6):
            fh.record(float(i), f"提示{i}")
        # get_recent_summary 默认 max_items=3
        summary = fh.get_recent_summary(6.0)
        parts = summary.split("、")
        assert len(parts) <= 3
        # 应包含最新的 3 条
        assert "提示5" in summary
        assert "提示4" in summary
        assert "提示3" in summary

    def test_multiple_entries_joined(self):
        fh = FastHistory()
        fh.record(1.0, "A")
        fh.record(2.0, "B")
        fh.record(3.0, "C")
        summary = fh.get_recent_summary(4.0)
        assert "A" in summary
        assert "B" in summary
        assert "C" in summary
        assert "、" in summary

    def test_empty_returns_none_string(self):
        fh = FastHistory()
        assert fh.get_recent_summary(0.0) == "无"

    def test_clear(self):
        fh = FastHistory()
        fh.record(1.0, "x")
        fh.clear()
        assert fh.get_recent_summary(1.0) == "无"
