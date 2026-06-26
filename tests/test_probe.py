"""浏览器探针 HTTP 端点"""

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.modules.setdefault("zmq", MagicMock())

from fastapi.testclient import TestClient

import backend.main as main_module
from backend.tts.queue import Priority


@pytest.fixture
def client():
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
        cfg.nitrogen_mock = True
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
        cfg.vlm_mock = True
        cfg.vlm_mock_delay_sec = 0.35
        cfg.global_tts_min_interval = 2.0
        mock_cfg.return_value = cfg
        mock_asr_cls.return_value = MagicMock()
        mock_nitro_factory.return_value = MagicMock()

        with TestClient(main_module.app) as c:
            yield c

    main_module._session = None


class TestProbeEndpoints:
    def test_probe_page_served(self, client):
        resp = client.get("/probe")
        assert resp.status_code == 200
        assert "E2E 链路探针" in resp.text

    def test_probe_health_idle(self, client):
        resp = client.get("/probe/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert "websocket_ready" in data
        assert data["nitrogen_mode"] in ("mock", "live")
        assert data["session_running"] is False

    def test_probe_tts_echo_without_session(self, client):
        resp = client.post("/probe/tts-echo")
        assert resp.status_code == 503

    def test_probe_tts_echo_queues_when_running(self, client):
        gs = MagicMock()
        gs._running = True
        gs.tts_queue = MagicMock()
        main_module._session = gs

        resp = client.post("/probe/tts-echo")
        assert resp.status_code == 200
        gs.tts_queue.push.assert_called_once()
        assert gs.tts_queue.push.call_args[0][1] == Priority.USER_ANSWER
