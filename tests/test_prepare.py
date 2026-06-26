"""POST /prepare 预热端点"""

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.modules.setdefault("zmq", MagicMock())

from fastapi.testclient import TestClient

import backend.main as main_module


@pytest.fixture
def client():
    with patch.object(main_module.warmup, "start_background_warmup", new_callable=AsyncMock) as mock_start, \
         patch.object(main_module.warmup, "get_status", return_value={
             "status": "idle",
             "whisper_ready": False,
             "tts_ready": False,
             "error": None,
         }):
        mock_start.return_value = None
        with TestClient(main_module.app) as c:
            yield c, mock_start


def test_prepare_triggers_background(client):
    c, mock_start = client
    resp = c.post("/prepare")
    assert resp.status_code == 200
    mock_start.assert_awaited_once()


def test_prepare_status(client):
    c, _ = client
    with patch.object(main_module.warmup, "get_status", return_value={
        "status": "ready",
        "whisper_ready": True,
        "tts_ready": True,
        "error": None,
    }):
        resp = c.get("/prepare/status")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ready"
        assert "vlm_mode" in resp.json()
