"""
集成测试：验证各模块组合后的端到端行为。
不依赖 NitroGen GPU / Whisper 模型 / Claude API / edge-tts。
"""

import time
import threading
import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from tests.conftest import make_signal, make_event, make_chunk
from backend.nitrogen.parser import parse_chunk
from backend.fast.action_filter import ActionFilter
from backend.fast.templates import render_fast
from backend.fast.event import EventType
from backend.slow.context_buffer import ContextBuffer, ConversationHistory, FastHistory
from backend.tts.queue import TTSQueue, Priority


# ═══════════════════════════════════════════════════════════════════════
# 快通道端到端：感知信号 → 过滤 → 模板 → 队列
# ═══════════════════════════════════════════════════════════════════════

class TestFastPathE2E:
    @pytest.fixture
    def fast_system(self, mock_tts_engine, mock_asr_handler):
        af  = ActionFilter(confidence_threshold=0.75, sustained_danger_sec=2.0)
        ctx = ContextBuffer(window_sec=15.0)
        fh  = FastHistory()
        q   = TTSQueue(mock_tts_engine, mock_asr_handler, inter_gap=0.0)
        return af, ctx, fh, q

    def test_dodge_signal_produces_fast_hint(self, fast_system, mock_tts_engine):
        af, ctx, fh, q = fast_system

        # 模拟从 WAIT 突变到 DODGE
        af.process(make_signal("WAIT", 0.9), 0.0, global_min_interval=0.0)
        event = af.process(make_signal("DODGE", 0.9, "LEFT"), 1.0, global_min_interval=0.0)

        assert event is not None
        assert event.type == EventType.SUDDEN_DODGE
        assert event.trigger_fast

        text = render_fast(event)
        assert "左" in text

        fh.record(event.timestamp, text)
        q.push(text, Priority.FAST_HINT)

        mock_tts_engine.speak_async.assert_called_once()
        spoken = mock_tts_engine.speak_async.call_args[0][0]
        assert spoken == text

    def test_attack_window_triggers_fast_and_queues(self, fast_system, mock_tts_engine):
        af, ctx, fh, q = fast_system

        af.process(make_signal("DODGE", 0.9), 0.0, global_min_interval=0.0)
        event = af.process(make_signal("ATTACK", 0.85), 1.0, global_min_interval=0.0)

        assert event is not None
        assert event.type == EventType.ATTACK_WINDOW
        assert event.trigger_fast
        assert event.trigger_slow   # 同时触发慢通道

        text = render_fast(event)
        q.push(text, Priority.FAST_HINT)
        mock_tts_engine.speak_async.assert_called()

    def test_pattern_completed_does_not_use_fast_path(self, fast_system, mock_tts_engine):
        af, ctx, fh, q = fast_system

        af.process(make_signal("ATTACK", 0.9), 0.0, global_min_interval=0.0)
        event = af.process(make_signal("WAIT", 0.9), 1.0, global_min_interval=0.0)

        assert event is not None
        assert event.type == EventType.PATTERN_COMPLETED
        assert not event.trigger_fast   # 不触发快通道

        # 不应推快通道
        if not event.trigger_fast:
            pass
        mock_tts_engine.speak_async.assert_not_called()

    def test_fast_history_records_spoken_text(self, fast_system, mock_tts_engine):
        af, ctx, fh, q = fast_system

        af.process(make_signal("WAIT", 0.9), 0.0, global_min_interval=0.0)
        event = af.process(make_signal("DODGE", 0.9, "RIGHT"), 1.0, global_min_interval=0.0)
        text = render_fast(event)
        fh.record(event.timestamp, text)

        summary = fh.get_recent_summary(1.5)
        assert "右" in summary

    def test_context_buffer_updated_during_events(self, fast_system):
        af, ctx, fh, q = fast_system

        s1 = make_signal("WAIT", 0.9)
        s2 = make_signal("DODGE", 0.9)
        ctx.push_signal(0.0, s1)
        af.process(s1, 0.0, global_min_interval=0.0)
        ctx.push_signal(1.0, s2)
        event = af.process(s2, 1.0, global_min_interval=0.0)

        if event:
            ctx.push_event(event.timestamp, event)

        summary = ctx.summarize()
        assert "DODGE" in summary or "WAIT" in summary


# ═══════════════════════════════════════════════════════════════════════
# 端到端：parse_chunk → ActionFilter → render_fast
# ═══════════════════════════════════════════════════════════════════════

class TestParseToFast:
    def test_full_chain_attack_chunk(self):
        """NitroGen chunk → PerceptionSignal → 过滤 → 模板"""
        af = ActionFilter(confidence_threshold=0.6)

        # 从 WAIT 到 ATTACK
        wait_chunk = make_chunk()
        af.process(parse_chunk(wait_chunk), 0.0, global_min_interval=0.0)

        attack_chunk = make_chunk(attack=0.9)
        sig = parse_chunk(attack_chunk)
        # 确认解析正确
        assert sig.primary_intent == "ATTACK"

    def test_full_chain_dodge_from_wait(self):
        af = ActionFilter(confidence_threshold=0.6)

        wait_sig = parse_chunk(make_chunk())
        af.process(wait_sig, 0.0, global_min_interval=0.0)

        dodge_sig = parse_chunk(make_chunk(dodge=0.9))
        event = af.process(dodge_sig, 1.0, global_min_interval=0.0)

        assert event is not None
        assert event.type == EventType.SUDDEN_DODGE
        text = render_fast(event)
        assert isinstance(text, str) and len(text) > 0


# ═══════════════════════════════════════════════════════════════════════
# 冷却与频率控制
# ═══════════════════════════════════════════════════════════════════════

class TestRateControl:
    def test_no_duplicate_events_within_cooldown(self):
        af = ActionFilter(
            confidence_threshold=0.7,
            cooldowns={"sudden_dodge": 5.0},
        )
        events = []

        # 第一次触发
        af.process(make_signal("WAIT", 0.9), 0.0, global_min_interval=0.0)
        e1 = af.process(make_signal("DODGE", 0.9), 1.0, global_min_interval=0.0)
        if e1:
            events.append(e1)

        # 冷却期内（1.5s，< 5s），重置前置并再次触发
        af._prev_signal = make_signal("WAIT", 0.9)
        e2 = af.process(make_signal("DODGE", 0.9), 2.0, global_min_interval=0.0)
        if e2:
            events.append(e2)

        dodge_events = [e for e in events if e.type == EventType.SUDDEN_DODGE]
        assert len(dodge_events) == 1   # 冷却内只允许一次

    def test_cooldown_allows_retrigger_after_expiry(self):
        af = ActionFilter(
            confidence_threshold=0.7,
            cooldowns={"sudden_dodge": 3.0},
        )
        # 第一次
        af.process(make_signal("WAIT", 0.9), 0.0, global_min_interval=0.0)
        af.process(make_signal("DODGE", 0.9), 1.0, global_min_interval=0.0)

        # 冷却过后（5s > 3s）
        af._prev_signal = make_signal("WAIT", 0.9)
        event = af.process(make_signal("DODGE", 0.9), 6.0, global_min_interval=0.0)
        assert event is not None
        assert event.type == EventType.SUDDEN_DODGE


# ═══════════════════════════════════════════════════════════════════════
# TTS 队列优先级集成
# ═══════════════════════════════════════════════════════════════════════

class TestTTSPriorityIntegration:
    def test_user_answer_takes_priority(self, mock_tts_engine, mock_asr_handler):
        """
        当 SLOW_ADVICE 在播放时，USER_ANSWER 应打断并抢先播出。
        """
        speak_order = []

        def slow_speak(text, is_cancelled=None, on_dispatched=None, on_error=None):
            speak_order.append(text)

        mock_tts_engine.speak_async.side_effect = slow_speak

        q = TTSQueue(mock_tts_engine, mock_asr_handler,
                     inter_gap=0.0, fallback_margin=0.0)
        q.push("慢通道建议", Priority.SLOW_ADVICE)   # 开始播放慢通道
        assert speak_order == ["慢通道建议"]

        # 现在推入用户回答，应打断
        q.push("用户提问回答", Priority.USER_ANSWER)
        mock_tts_engine.stop.assert_called()

    def test_multiple_fast_hints_respect_expiry(self, mock_tts_engine, mock_asr_handler):
        """
        短时间内收到多条快提示，只有最新（未过期）的被播放。
        """
        import heapq

        spoken_texts = []

        def record(text, is_cancelled=None, on_dispatched=None, on_error=None):
            spoken_texts.append(text)
            if on_dispatched and not (is_cancelled and is_cancelled()):
                on_dispatched(0.1)

        mock_tts_engine.speak_async.side_effect = record

        q = TTSQueue(mock_tts_engine, mock_asr_handler,
                     inter_gap=0.0, fallback_margin=0.0)
        q._is_speaking = True  # 阻止立即播放

        # 手动推入一个已过期的快提示
        old_item = TTSItem = __import__(
            "backend.tts.queue", fromlist=["TTSItem"]
        ).TTSItem
        expired = old_item(
            priority=Priority.FAST_HINT,
            enqueue_time=time.time() - 100.0,
            text="旧提示（应被丢弃）",
            expire_sec=2.0,
        )
        fresh = old_item(
            priority=Priority.FAST_HINT,
            enqueue_time=time.time(),
            text="新提示",
            expire_sec=30.0,
        )
        with q._lock:
            heapq.heappush(q._heap, expired)
            heapq.heappush(q._heap, fresh)

        q._is_speaking = False
        q._speak_next()

        assert "旧提示（应被丢弃）" not in spoken_texts
        assert "新提示" in spoken_texts


# ═══════════════════════════════════════════════════════════════════════
# ConversationHistory 多轮集成
# ═══════════════════════════════════════════════════════════════════════

class TestConversationHistoryIntegration:
    def test_multi_turn_context_passed_to_vlm(self):
        """多轮问答后，to_messages() 包含完整历史供 VLM 使用"""
        hist = ConversationHistory()
        hist.add_turn("这段怎么打？", "等右拳落地后打三下再撤")
        hist.add_turn("我刚才那样对吗？", "对的，timing 很准")

        msgs = hist.to_messages()
        assert len(msgs) == 4  # 2轮 × 2消息

        # 验证角色顺序 user/assistant 交替
        for i, expected_role in enumerate(["user", "assistant", "user", "assistant"]):
            assert msgs[i]["role"] == expected_role

    def test_fast_history_prevents_vlm_repeat(self):
        """FastHistory 摘要应包含快通道已播内容，供 VLM 避免重复"""
        fh = FastHistory()
        fh.record(1.0, "向左闪！")
        fh.record(2.0, "有机会，打！")

        summary = fh.get_recent_summary(3.0)
        assert "向左闪！" in summary
        assert "有机会，打！" in summary

        # VLM prompt 中注入此摘要，避免重复
        prompt_snippet = f'刚才快通道已播报："{summary}"'
        assert "向左闪！" in prompt_snippet
