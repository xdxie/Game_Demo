"""
测试 TTSEngine 合成超时。
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from backend.tts.engine import TTSEngine


class TestTTSEngineTimeout:
    def test_synthesis_timeout_triggers_on_error(self):
        engine = TTSEngine(synthesis_timeout=0.05)
        errors = []

        async def slow_synthesize(text):
            await asyncio.sleep(1.0)
            return b"mp3"

        with patch.object(engine, "_synthesize_cached", side_effect=slow_synthesize):
            engine.speak_async(
                "测试超时",
                on_error=lambda: errors.append(True),
            )

        import time
        deadline = time.time() + 2.0
        while not errors and time.time() < deadline:
            time.sleep(0.05)

        assert len(errors) == 1

    def test_cached_synthesis_not_timed_out(self):
        engine = TTSEngine(synthesis_timeout=0.05)
        engine._cache["快"] = b"cached"
        dispatched = []

        engine.speak_async("快", on_dispatched=lambda d: dispatched.append(d))

        import time
        deadline = time.time() + 1.0
        while not dispatched and time.time() < deadline:
            time.sleep(0.02)

        assert len(dispatched) == 1
