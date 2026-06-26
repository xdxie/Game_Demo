"""测试 VLMRequestManager seek generation 校验"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.fast.event import EventType, GameEvent
from tests.conftest import make_event, make_signal


@pytest.fixture
def vlm_manager():
    from backend.slow.trigger import VLMRequestManager

    tts = MagicMock()
    ctx = MagicMock()
    ctx.summarize.return_value = ""
    fast_hist = MagicMock()
    fast_hist.get_recent_summary.return_value = None
    conv = MagicMock()
    conv.to_messages.return_value = []

    seek_gen = {"value": 0}

    mgr = VLMRequestManager(
        tts_queue=tts,
        context_buffer=ctx,
        fast_history=fast_hist,
        conversation_history=conv,
        get_seek_generation=lambda: seek_gen["value"],
    )
    mgr._seek_gen = seek_gen
    return mgr


def make_user_event(text: str = "这是什么?"):
    return GameEvent(
        type=EventType.USER_QUESTION,
        timestamp=12.0,
        perception=make_signal(),
        trigger_fast=False,
        trigger_slow=True,
        user_text=text,
    )


class TestVLMSeekGeneration:
    def test_submit_discards_stale_utterance_seek_gen(self, vlm_manager):
        async def _run():
            vlm_manager._seek_gen["value"] = 1
            frame = MagicMock()
            event = make_user_event()

            with patch("backend.slow.trigger.call_vlm", new_callable=AsyncMock) as mock_vlm:
                await vlm_manager.submit(event, frame, utterance_seek_gen=0)
                await asyncio.sleep(0.05)
                mock_vlm.assert_not_called()

        asyncio.run(_run())

    def test_run_discards_stale_seek_gen_before_push(self, vlm_manager):
        async def _run():
            frame = MagicMock()
            event = make_user_event()
            started = asyncio.Event()
            release = asyncio.Event()

            async def slow_vlm(**kwargs):
                started.set()
                await release.wait()
                return "回答"

            with patch("backend.slow.trigger.call_vlm", side_effect=slow_vlm):
                await vlm_manager.submit(event, frame, utterance_seek_gen=0)
                await started.wait()
                vlm_manager._seek_gen["value"] = 1
                release.set()
                await asyncio.sleep(0.05)

            vlm_manager._tts.push.assert_not_called()

        asyncio.run(_run())

    def test_submit_without_seek_gen_always_runs(self, vlm_manager):
        async def _run():
            frame = MagicMock()
            event = make_event(etype=EventType.SUDDEN_DODGE, slow=True)

            with patch(
                "backend.slow.trigger.call_vlm",
                new_callable=AsyncMock,
                return_value="快躲",
            ):
                await vlm_manager.submit(
                    event, frame, utterance_seek_gen=vlm_manager._seek_gen["value"],
                )
                await asyncio.sleep(0.05)

            vlm_manager._tts.push.assert_called_once()

        asyncio.run(_run())

    def test_event_submit_discarded_after_seek(self, vlm_manager):
        async def _run():
            frame = MagicMock()
            event = make_event(etype=EventType.SUDDEN_DODGE, slow=True)
            submit_gen = vlm_manager._seek_gen["value"]

            with patch(
                "backend.slow.trigger.call_vlm",
                new_callable=AsyncMock,
                return_value="慢建议",
            ):
                await vlm_manager.submit(
                    event, frame, utterance_seek_gen=submit_gen,
                )
                vlm_manager._seek_gen["value"] = submit_gen + 1
                await asyncio.sleep(0.05)

            vlm_manager._tts.push.assert_not_called()

        asyncio.run(_run())
