"""NitroGen 客户端工厂路由"""

import os
from unittest.mock import patch

from backend.config import Config
from backend.nitrogen.factory import (
    create_nitrogen_client,
    nitrogen_analysis_fps,
    nitrogen_backend,
    nitrogen_mode_label,
)
from backend.nitrogen.mock_client import MockNitroGenClient


def test_mock_when_nitrogen_mock_enabled():
    cfg = Config(nitrogen_mock=True)
    assert nitrogen_backend(cfg) == "mock"
    assert isinstance(create_nitrogen_client(cfg), MockNitroGenClient)


def test_fast_api_backend():
    cfg = Config(nitrogen_mock=False, nitrogen_backend="fast_api")
    with patch.dict(os.environ, {"NITROGEN_MOCK": "0", "NITROGEN_BACKEND": "fast_api"}):
        assert nitrogen_mode_label(cfg) == "fast_api"
        client = create_nitrogen_client(cfg)
        assert client.backend == "fast_api"


def test_analysis_fps_uses_fast_api_setting():
    cfg = Config(nitrogen_mock=False, nitrogen_backend="fast_api", nitrogen_fast_api_fps=2.5)
    with patch.dict(os.environ, {"NITROGEN_MOCK": "0", "NITROGEN_BACKEND": "fast_api"}):
        assert nitrogen_analysis_fps(cfg) == 2.5
