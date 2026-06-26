"""HTTP 会话端点：/start 冲突、/session/status"""

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.modules.setdefault("zmq", MagicMock())

from fastapi.testclient import TestClient

import backend.main as main_module


@pytest.fixture
def http_client():
    main_module._session = None
    main_module._ws_clients.clear()
    main_module._ws_roles.clear()
    main_module._primary_ws = None

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
        cfg.global_tts_min_interval = 2.0
        mock_cfg.return_value = cfg

        mock_asr_cls.return_value = MagicMock()
        mock_nitro_factory.return_value = MagicMock()

        with TestClient(main_module.app) as client:
            yield client

    main_module._session = None


class TestHttpSession:
    def test_session_status_idle(self, http_client):
        resp = http_client.get("/session/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["running"] is False
        assert data["has_primary"] is False
        assert "vlm_mode" in data

    def test_start_returns_409_when_already_running(self, http_client):
        gs = MagicMock()
        gs._running = True
        gs.stop = AsyncMock()
        gs.start = AsyncMock()
        main_module._session = gs

        resp = http_client.post("/start")
        assert resp.status_code == 409
        assert resp.json()["status"] == "already_running"
        gs.stop.assert_not_called()

    def test_start_creates_session_when_idle(self, http_client):
        with patch.object(main_module, "GameSession") as mock_gs_cls:
            instance = MagicMock()
            instance._running = False
            instance.start = AsyncMock()
            mock_gs_cls.return_value = instance

            resp = http_client.post("/start")
            assert resp.status_code == 200
            mock_gs_cls.assert_called_once()
            instance.start.assert_awaited_once()
