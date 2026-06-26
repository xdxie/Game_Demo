"""
公共 fixtures 和辅助函数。
所有测试均可在不启动 NitroGen / Whisper / Claude / edge-tts 的情况下运行。
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pytest
from unittest.mock import MagicMock, patch

from backend.nitrogen.parser import PerceptionSignal
from backend.fast.event import EventType, GameEvent


def make_chunk(
    attack: float = 0.0,
    dodge: float  = 0.0,
    jx: float     = 0.0,
    jy: float     = 0.0,
) -> dict:
    buttons = np.zeros((16, 21), dtype=np.float32)
    j_left  = np.zeros((16, 2),  dtype=np.float32)

    if attack > 0:
        buttons[:, 5]  = attack
        buttons[:, 18] = attack
        buttons[:, 16] = attack

    if dodge > 0:
        buttons[:, 9]  = dodge
        buttons[:, 7]  = dodge
        buttons[:, 14] = dodge

    j_left[:, 0] = jx
    j_left[:, 1] = jy

    return {
        "j_left":  j_left,
        "j_right": np.zeros((16, 2), dtype=np.float32),
        "buttons": buttons,
    }


def make_signal(
    intent:    str   = "WAIT",
    confidence: float = 0.8,
    direction: str | None = None,
    magnitude: float = 0.0,
) -> PerceptionSignal:
    return PerceptionSignal(
        primary_intent=intent,
        confidence=confidence,
        move_direction=direction,
        move_magnitude=magnitude,
        horizon_sequence=[f"{intent}×16"],
    )


def make_event(
    etype:     EventType = EventType.SUDDEN_DODGE,
    timestamp: float     = 0.0,
    signal:    PerceptionSignal | None = None,
    fast:      bool = True,
    slow:      bool = False,
) -> GameEvent:
    if signal is None:
        signal = make_signal("DODGE", 0.9)
    return GameEvent(
        type=etype,
        timestamp=timestamp,
        perception=signal,
        trigger_fast=fast,
        trigger_slow=slow,
    )


@pytest.fixture
def mock_tts_engine():
    """
    TTSEngine mock：speak_async 同步调用 on_dispatched（模拟音频已发出）。
    播放完成需测试显式调用 queue.on_client_tts_done(uid)。
    """
    engine = MagicMock()
    engine.on_audio_data = None

    def _speak(text, on_dispatched=None, on_error=None):
        if on_dispatched:
            on_dispatched(0.5)

    engine.speak_async.side_effect = _speak
    return engine


@pytest.fixture
def mock_asr_handler():
    """最简 ASRHandler mock"""
    asr = MagicMock()
    asr._muted = False
    return asr
