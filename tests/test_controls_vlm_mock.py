"""简化操控量与 mock VLM"""

import asyncio

import pytest

from backend.nitrogen.controls import signal_from_controls
from backend.fast.event import EventType, GameEvent
from backend.slow.vlm_mock import call_vlm_mock


class TestControls:
    def test_brake_signal(self):
        s = signal_from_controls(0.0, 0, 1)
        assert s.brake == 1
        assert s.throttle == 0
        assert s.primary_intent == "WAIT"

    def test_left_throttle(self):
        s = signal_from_controls(-0.8, 1, 0)
        assert s.steer == pytest.approx(-0.8)
        assert s.throttle == 1
        assert s.move_direction == "LEFT"


def test_vlm_mock_user_question():
    signal = signal_from_controls(-0.5, 1, 0)
    event = GameEvent(
        type=EventType.USER_QUESTION,
        timestamp=1.0,
        perception=signal,
        trigger_fast=False,
        trigger_slow=True,
        user_text="现在该怎么开",
    )
    text = asyncio.run(call_vlm_mock(
        event, "现在该怎么开", actions_timeline_text="关键动作时间线", delay_sec=0,
    ))
    assert "现在该怎么开" in text
    assert "转向" not in text

    text_n = asyncio.run(call_vlm_mock(
        event, "现在该怎么开", delay_sec=0, include_nitrogen=True,
    ))
    assert "转向" in text_n
