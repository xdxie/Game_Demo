"""NitroGen 模拟客户端与工厂"""

import io
import time
from unittest.mock import MagicMock

import pytest
from PIL import Image

from backend.config import Config
from backend.nitrogen.factory import create_nitrogen_client, nitrogen_mock_enabled
from backend.nitrogen.mock_client import MockNitroGenClient
from backend.video.frame_buffer import FrameBuffer


def _tiny_jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color=(40, 80, 120)).save(buf, format="JPEG")
    return buf.getvalue()


class TestNitrogenFactory:
    def test_default_config_uses_mock(self):
        cfg = Config()
        assert cfg.nitrogen_mock is True
        assert nitrogen_mock_enabled(cfg) is True

    def test_env_overrides_config(self, monkeypatch):
        cfg = Config(nitrogen_mock=True)
        monkeypatch.setenv("NITROGEN_MOCK", "0")
        assert nitrogen_mock_enabled(cfg) is False

    def test_create_mock_client(self):
        client = create_nitrogen_client(Config(nitrogen_mock=True))
        assert isinstance(client, MockNitroGenClient)
        assert client.is_mock is True


class TestMockNitroGenClient:
    def test_emits_signal_when_frame_available(self):
        fb = FrameBuffer()
        fb.push(_tiny_jpeg(), 1.0)
        client = MockNitroGenClient(cycle_sec=0.05)
        client.start(fb)
        try:
            deadline = time.time() + 2.0
            while time.time() < deadline:
                if client.latest_signal is not None:
                    break
                time.sleep(0.02)
            assert client.latest_signal is not None
            assert client.inference_count >= 1
            assert client.timeout_count == 0
        finally:
            client.stop()

    def test_on_frame_pushed_sets_signal_immediately(self):
        fb = FrameBuffer()
        client = MockNitroGenClient()
        client.start(fb)
        try:
            fb.push(_tiny_jpeg(), 1.0)
            client.on_frame_pushed()
            assert client.latest_signal is not None
            assert client.latest_signal.primary_intent == "NAVIGATE"
            assert client.latest_signal.steer < 0
            assert client.latest_signal.throttle == 1
            assert client.inference_count == 1
        finally:
            client.stop()

    def test_no_signal_without_frame(self):
        fb = FrameBuffer()
        client = MockNitroGenClient(cycle_sec=0.05)
        client.start(fb)
        try:
            time.sleep(0.15)
            assert client.latest_signal is None
            assert client.inference_count == 0
        finally:
            client.stop()
