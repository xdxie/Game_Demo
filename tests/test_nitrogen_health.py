"""NitroGen FastAPI 健康探针"""

from unittest.mock import MagicMock, patch

import httpx
import pytest

from backend.nitrogen.health import check_fast_api_health


class TestNitrogenHealth:
    def test_port_closed(self):
        with patch("backend.nitrogen.health.port_is_open", return_value=False):
            r = check_fast_api_health("http://localhost:18000", probe_predict=False)
        assert r["ok"] is False
        assert r["port_open"] is False
        assert "未监听" in r["message"]

    def test_reset_fails(self):
        with patch("backend.nitrogen.health.port_is_open", return_value=True), \
             patch("httpx.Client") as mock_client_cls:
            client = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = client
            resp = MagicMock()
            resp.status_code = 404
            client.post.return_value = resp
            r = check_fast_api_health("http://localhost:18000", probe_predict=False)
        assert r["port_open"] is True
        assert r["reset_ok"] is False
        assert r["ok"] is False

    def test_full_probe_ok(self):
        with patch("backend.nitrogen.health.port_is_open", return_value=True), \
             patch("httpx.Client") as mock_client_cls:
            client = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = client

            reset_resp = MagicMock(status_code=200)
            predict_resp = MagicMock(
                status_code=200,
                json=lambda: {"frame_idx": 0, "action_summary": {}},
            )
            client.post.side_effect = [reset_resp, predict_resp]
            r = check_fast_api_health("http://localhost:18000", probe_predict=True)
        assert r["ok"] is True
        assert r["reset_ok"] is True
        assert r["predict_ok"] is True

    def test_connect_error(self):
        with patch("backend.nitrogen.health.port_is_open", return_value=True), \
             patch("httpx.Client") as mock_client_cls:
            client = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = client
            client.post.side_effect = httpx.ConnectError("refused")
            r = check_fast_api_health("http://localhost:18000")
        assert r["ok"] is False
        assert "连接失败" in r["message"]
