"""测试 GameSession seek/pause/stop 控制逻辑"""

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.modules.setdefault("zmq", MagicMock())

import backend.main as main_module
from backend.fast.event import EventType


@pytest.fixture
def session():
    with patch.object(main_module, "create_nitrogen_client") as mock_nitro_factory, \
         patch.object(main_module, "ASRHandler") as mock_asr_cls, \
         patch.object(main_module, "TTSEngine"), \
         patch.object(main_module, "get_config") as mock_cfg:

        cfg = MagicMock()
        cfg.nitrogen_server = "tcp://localhost:5555"
        cfg.fast_trigger_confidence = 0.75
        cfg.sustained_danger_sec = 3.0
        cfg.cooldowns = {}
        cfg.context_window_sec = 15.0
        cfg.vlm_model = "claude-sonnet-4-6"
        cfg.vlm_max_tokens = 120
        cfg.tts_voice = "zh-CN-YunxiNeural"
        cfg.tts_rate = "+20%"
        cfg.tts_inter_utterance_gap = 0.8
        cfg.tts_done_fallback_margin = 1.0
        cfg.tts_synthesis_timeout_sec = 15.0
        cfg.whisper_model = "base"
        cfg.whisper_language = "zh"
        cfg.vad_silence_threshold = 300
        cfg.vad_speech_min_sec = 0.5
        cfg.vad_silence_end_sec = 1.2
        cfg.tts_mute_tail_sec = 0.2
        cfg.nitrogen_target_fps = 10.0
        cfg.fast_hint_expire_sec = 2.0
        cfg.slow_max_queue_age = 8.0
        cfg.vlm_dedup_sec = 5.0
        mock_cfg.return_value = cfg

        mock_asr = MagicMock()
        mock_asr.seek_generation = 0
        mock_asr_cls.return_value = mock_asr

        mock_nitro = MagicMock()
        mock_nitro_factory.return_value = mock_nitro

        gs = main_module.GameSession()
        gs.tts_queue = MagicMock()
        gs._loop = asyncio.new_event_loop()
        gs.vlm_manager.cancel_all = AsyncMock()
        gs._broadcast = AsyncMock()
        yield gs
        gs._loop.close()


class TestGameSessionControls:
    def test_on_seek_resets_signal_and_time(self, session):
        session._analysis_paused = True
        session.conv_hist.add_turn("问题", "回答")
        session.fast_hist.record(1.0, "快提示")
        async def _run():
            await session.on_seek(42.5)

        asyncio.run(_run())
        session.nitrogen.clear_signal.assert_called_once()
        session.asr_handler.reset_for_seek.assert_called_once()
        session.asr_handler.force_unmute.assert_called_once()
        session.tts_queue.clear_and_stop.assert_called_once()
        session.vlm_manager.cancel_all.assert_called_once()
        assert len(session.conv_hist) == 1
        assert session.fast_hist.get_recent_summary(2.0) == "无"
        assert session.frame_buffer.video_position == 42.5
        assert session._analysis_paused is True

    def test_on_clear_conversation(self, session):
        session.conv_hist.add_turn("问题", "回答")
        async def _run():
            await session.on_clear_conversation()

        asyncio.run(_run())
        assert len(session.conv_hist) == 0
        session._broadcast.assert_called_once()
        assert session._broadcast.call_args[0][0]["type"] == "conversation_cleared"

    def test_on_seek_restores_analysis_running_after_seek(self, session):
        session._analysis_paused = False
        async def _run():
            await session.on_seek(12.0)

        asyncio.run(_run())
        assert session._analysis_paused is False

    def test_on_pause_stops_tts_without_muting_asr(self, session):
        async def _run():
            await session.on_pause()

        asyncio.run(_run())
        assert session._analysis_paused is True
        session.tts_queue.clear_and_stop.assert_called_once()
        session.vlm_manager.cancel_all.assert_called_once()
        session.asr_handler.mute.assert_not_called()

    def test_on_resume_clears_analysis_paused(self, session):
        session._analysis_paused = True
        async def _run():
            await session.on_resume()

        asyncio.run(_run())
        assert session._analysis_paused is False
        session.asr_handler.force_unmute.assert_called_once()

    def test_handle_user_utterance_discards_after_seek(self, session):
        async def _run():
            session.asr_handler.seek_generation = 2
            session.vlm_manager.submit = AsyncMock()
            await session._handle_user_utterance("旧问题", utterance_gen=1)

        asyncio.run(_run())
        session._broadcast.assert_not_called()
        session.vlm_manager.submit.assert_not_called()

    def test_handle_user_utterance_submits_when_seek_gen_valid(self, session):
        async def _run():
            session.asr_handler.seek_generation = 1
            session.frame_buffer.latest_frame = MagicMock()
            session.vlm_manager.submit = AsyncMock()
            await session._handle_user_utterance("新问题", utterance_gen=1)

        asyncio.run(_run())
        session._broadcast.assert_called_once()
        session.vlm_manager.submit.assert_called_once()
        assert session.vlm_manager.submit.call_args.kwargs["utterance_seek_gen"] == 1

    def test_handle_user_utterance_skips_vlm_without_frame(self, session):
        async def _run():
            session.asr_handler.seek_generation = 1
            session.frame_buffer.latest_frame = None
            session.vlm_manager.submit = AsyncMock()
            await session._handle_user_utterance("无画面问题", utterance_gen=1)

        asyncio.run(_run())
        session._broadcast.assert_called()
        session.vlm_manager.submit.assert_not_called()

    def test_stop_force_unmutes_asr(self, session):
        async def _run():
            session._main_loop_task = None
            session._running = False
            await session.stop()

        asyncio.run(_run())
        session.asr_handler.force_unmute.assert_called_once()

    def test_on_video_ended_pauses_analysis(self, session):
        async def _run():
            await session.on_video_ended()

        asyncio.run(_run())
        assert session._analysis_paused is True
        session.tts_queue.clear_and_stop.assert_called_once()
        session.vlm_manager.cancel_all.assert_called_once()
        session._broadcast.assert_called()
