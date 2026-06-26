"""WebSocket 端点：register 握手、主连接选举、输入门控"""

import json
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.modules.setdefault("zmq", MagicMock())

from fastapi.testclient import TestClient

import backend.main as main_module


def _reset_ws_state():
    main_module._ws_clients.clear()
    main_module._ws_roles.clear()
    main_module._primary_ws = None
    main_module._session = None


@pytest.fixture
def ws_client():
    _reset_ws_state()
    with TestClient(main_module.app) as client:
        yield client
    _reset_ws_state()


def _register(ws, role="player"):
    ws.send_text(json.dumps({"type": "register", "role": role}))
    msg = json.loads(ws.receive_text())
    assert msg["type"] == "session_role"
    return msg["role"]


class TestWebSocketHelpers:
    def test_remove_dead_ws_promotes_next_player(self):
        _reset_ws_state()
        ws1 = MagicMock()
        ws2 = MagicMock()
        main_module._ws_clients.extend([ws1, ws2])
        main_module._ws_roles = {ws1: "player", ws2: "player"}
        main_module._primary_ws = ws1

        main_module._remove_dead_ws_clients([ws1])

        assert ws1 not in main_module._ws_clients
        assert main_module._primary_ws is ws2

    def test_remove_dead_ws_clears_primary_when_no_players(self):
        _reset_ws_state()
        ws1 = MagicMock()
        main_module._ws_clients.append(ws1)
        main_module._ws_roles = {ws1: "observer"}
        main_module._primary_ws = ws1

        main_module._remove_dead_ws_clients([ws1])

        assert main_module._primary_ws is None


class TestWebSocketRegister:
    def test_player_register_becomes_primary(self, ws_client):
        with ws_client.websocket_connect("/ws") as ws:
            role = _register(ws, "player")
            assert role == "primary"
            assert main_module._primary_ws is not None

    def test_observer_register_is_not_primary(self, ws_client):
        with ws_client.websocket_connect("/ws") as ws:
            role = _register(ws, "observer")
            assert role == "observer"
            assert main_module._primary_ws is None

    def test_second_player_is_observer(self, ws_client):
        with ws_client.websocket_connect("/ws") as ws1:
            assert _register(ws1, "player") == "primary"
            with ws_client.websocket_connect("/ws") as ws2:
                assert _register(ws2, "player") == "observer"

    def test_primary_client_tts_done_accepted(self, ws_client):
        mock_session = MagicMock()
        mock_session._broadcast = AsyncMock()
        main_module._session = mock_session

        with ws_client.websocket_connect("/ws") as ws:
            _register(ws, "player")
            ws.send_text(json.dumps({
                "type": "tts_done",
                "utterance_id": 8,
            }))

        mock_session.tts_queue.on_client_tts_done.assert_called_once_with(8)

    def test_observer_tts_done_ignored(self, ws_client):
        mock_session = MagicMock()
        mock_session._broadcast = AsyncMock()
        main_module._session = mock_session

        with ws_client.websocket_connect("/ws") as primary:
            _register(primary, "player")
            with ws_client.websocket_connect("/ws") as observer:
                _register(observer, "observer")
                observer.send_text(json.dumps({
                    "type": "tts_done",
                    "utterance_id": 7,
                }))
                primary.send_text(json.dumps({
                    "type": "tts_done",
                    "utterance_id": 9,
                }))

        mock_session.tts_queue.on_client_tts_done.assert_called_once_with(9)

    def test_reassign_primary_from_players(self):
        _reset_ws_state()
        ws_a = MagicMock()
        ws_b = MagicMock()
        main_module._ws_clients = [ws_a, ws_b]
        main_module._ws_roles = {ws_a: "player", ws_b: "player"}
        main_module._primary_ws = None

        promoted = main_module._reassign_primary_from_players()

        assert promoted is ws_a
