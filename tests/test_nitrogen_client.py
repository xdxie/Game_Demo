"""测试 NitroGenClient 辅助方法"""

import sys
from unittest.mock import MagicMock

sys.modules.setdefault("zmq", MagicMock())

from backend.nitrogen.client import NitroGenClient
from backend.nitrogen.parser import PerceptionSignal


def _make_client() -> NitroGenClient:
    client = NitroGenClient.__new__(NitroGenClient)
    client._signal_lock = __import__("threading").Lock()
    client._latest_signal = None
    client._signal_generation = 0
    return client


def test_clear_signal_removes_latest():
    client = _make_client()
    client._latest_signal = PerceptionSignal(
        primary_intent="ATTACK",
        confidence=0.9,
        move_direction=None,
        move_magnitude=0.0,
    )
    client.clear_signal()
    assert client.latest_signal is None
    assert client._signal_generation == 1


def test_stale_inference_write_discarded_after_clear_signal():
    """在途推理完成时若 generation 已变，不应写回 latest_signal"""
    client = _make_client()
    stale_signal = PerceptionSignal(
        primary_intent="DODGE",
        confidence=0.9,
        move_direction=None,
        move_magnitude=0.0,
    )
    gen_at_start = client._signal_generation
    client.clear_signal()

    with client._signal_lock:
        if gen_at_start == client._signal_generation:
            client._latest_signal = stale_signal

    assert client.latest_signal is None
