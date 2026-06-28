"""慢系统 VLM 不被快通道 skip 阻塞。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest

from backend.fast.action_filter import ActionFilter
from backend.fast.event import EventType, GameEvent
from backend.fast.game_vocab import WUKONG_GAME_ID
from backend.main import GameSession
from tests.conftest import make_signal


def _dual_flag_event() -> GameEvent:
    return GameEvent(
        type=EventType.ATTACK_WINDOW,
        timestamp=10.0,
        perception=make_signal("ATTACK", 0.9),
        trigger_fast=True,
        trigger_slow=True,
    )


def _mock_session(*, fast_tts_enabled: bool = True) -> MagicMock:
    session = MagicMock()
    session.cfg.fast_tts_enabled = fast_tts_enabled
    session.current_game_id = WUKONG_GAME_ID
    session.asr_handler.seek_generation = 0
    session.frame_buffer.latest_frame = MagicMock()
    session.vlm_manager.submit = AsyncMock()
    session.ctx_buffer.push_event = MagicMock()
    session.fast_hist.record = MagicMock()
    session.tts_queue.push = MagicMock()
    session.fast_output_gate.should_speak.return_value = True
    session.fast_output_gate.tts_priority.return_value = MagicMock()
    session._tlog = MagicMock()
    return session


class TestHandleEventSlowNotBlocked:
    def test_vlm_submit_when_fast_text_empty(self):
        async def _run():
            session = _mock_session()
            event = _dual_flag_event()
            with patch("backend.main.render_fast", return_value=""):
                await GameSession._handle_event(session, event)
            session.tts_queue.push.assert_not_called()
            session.vlm_manager.submit.assert_awaited_once_with(
                event, session.frame_buffer.latest_frame, utterance_seek_gen=0,
            )
        asyncio.run(_run())

    def test_vlm_submit_when_fast_gate_blocks(self):
        async def _run():
            session = _mock_session()
            session.fast_output_gate.should_speak.return_value = False
            event = _dual_flag_event()
            with patch("backend.main.render_fast", return_value="反击！"):
                await GameSession._handle_event(session, event)
            session.tts_queue.push.assert_not_called()
            session.vlm_manager.submit.assert_awaited_once()
        asyncio.run(_run())

    def test_fast_and_slow_both_fire_when_allowed(self):
        async def _run():
            session = _mock_session()
            event = _dual_flag_event()
            with patch("backend.main.render_fast", return_value="反击！"):
                await GameSession._handle_event(session, event)
            session.tts_queue.push.assert_called_once()
            session.vlm_manager.submit.assert_awaited_once()
        asyncio.run(_run())


class TestWukongSlowDetect:
    @pytest.fixture
    def af(self):
        return ActionFilter(
            confidence_threshold=0.75,
            sustained_danger_sec=2.0,
        )

    def test_wukong_pattern_completed_triggers_slow(self, af):
        af.process(make_signal("ATTACK", 0.9), 0.0, global_min_interval=0.0,
                   game_id=WUKONG_GAME_ID)
        event = af.process(
            make_signal("WAIT", 0.9), 1.0,
            global_min_interval=0.0,
            game_id=WUKONG_GAME_ID,
        )
        assert event is not None
        assert event.type == EventType.PATTERN_COMPLETED
        assert event.trigger_slow is True
        assert event.trigger_fast is False

    def test_wukong_does_not_restore_sudden_dodge(self, af):
        af.process(make_signal("WAIT", 0.9), 0.0, global_min_interval=0.0,
                   game_id=WUKONG_GAME_ID)
        event = af.process(
            make_signal("DODGE", 0.9), 1.0,
            global_min_interval=0.0,
            game_id=WUKONG_GAME_ID,
        )
        assert event is None or event.type != EventType.SUDDEN_DODGE
