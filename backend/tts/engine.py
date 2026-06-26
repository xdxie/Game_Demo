"""
TTS 引擎封装（edge-tts）。

Fix 14：TTS 音频改为发送给前端播放，不再在服务端用 pygame 播放。
播放完成时序由 TTSQueue 通过前端 tts_done + fallback 定时器管理。
"""

from __future__ import annotations
import asyncio
import io
import logging
import threading
from typing import Callable, Optional

import edge_tts

logger = logging.getLogger(__name__)

PRELOAD_TEXTS = ["向左闪！", "注意，快闪！", "有机会，打！", "进攻！", "注意！"]


class TTSEngine:
    """
    edge-tts 封装。
    合成 MP3 bytes → 调用 on_audio_data 发往前端。
    on_dispatched(estimated_duration) 在音频发出后回调，供队列设置 fallback 定时器。
    """

    def __init__(self, voice: str = "zh-CN-YunxiNeural", rate: str = "+20%"):
        self.voice = voice
        self.rate  = rate

        self._stop_flag = threading.Event()
        self._cache: dict[str, bytes] = {}

        self.on_audio_data: Optional[Callable[[bytes], None]] = None

    def preload(self, texts: list[str] | None = None):
        """预合成常用短语，存入内存缓存"""
        targets = texts or PRELOAD_TEXTS
        for text in targets:
            try:
                asyncio.run(self._async_preload(text))
            except RuntimeError:
                logger.warning("TTS preload skipped (event loop conflict): %s", text)
        logger.info("TTS preloaded %d phrases", len(self._cache))

    async def _async_preload(self, text: str):
        try:
            data = await self._synthesize(text)
            self._cache[text] = data
        except Exception as e:
            logger.warning("Preload failed for '%s': %s", text, e)

    def speak_async(
        self,
        text: str,
        on_dispatched: Optional[Callable[[float], None]] = None,
        on_error: Optional[Callable[[], None]] = None,
    ):
        """
        异步合成并发送（在新线程中执行，不阻塞调用方）。
        合成完成后：
          1. 调用 on_audio_data(mp3_bytes) → 发往前端播放
          2. 调用 on_dispatched(estimated_duration) → 队列设置 fallback 定时器
        """
        self._stop_flag.clear()

        threading.Thread(
            target=self._speak_thread,
            args=(text, on_dispatched, on_error),
            daemon=True,
            name="tts-speak",
        ).start()

    def stop(self):
        """立即中止当前合成/发送流程"""
        self._stop_flag.set()

    # ── 内部实现 ──────────────────────────────────────────────────────

    def _speak_thread(
        self,
        text: str,
        on_dispatched: Optional[Callable[[float], None]],
        on_error: Optional[Callable[[], None]],
    ):
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            audio_data = loop.run_until_complete(self._synthesize_cached(text))
        except Exception as e:
            logger.error("TTS synthesis error: %s", e)
            if on_error:
                on_error()
            return

        if self._stop_flag.is_set():
            return

        if not audio_data:
            if on_error:
                on_error()
            return

        if self.on_audio_data:
            try:
                self.on_audio_data(audio_data)
            except Exception as e:
                logger.error("TTS on_audio_data callback error: %s", e)

        if self._stop_flag.is_set():
            return

        if on_dispatched:
            duration = self._estimate_duration(audio_data)
            logger.debug("TTS dispatched, estimated duration: %.2fs for '%s'",
                         duration, text[:20])
            try:
                on_dispatched(duration)
            except Exception as e:
                logger.error("TTS on_dispatched callback error: %s", e)

    async def _synthesize_cached(self, text: str) -> bytes:
        if text in self._cache:
            return self._cache[text]
        data = await self._synthesize(text)
        self._cache[text] = data
        return data

    async def _synthesize(self, text: str) -> bytes:
        """调用 edge-tts API 合成音频，返回 MP3 bytes"""
        communicate = edge_tts.Communicate(text, voice=self.voice, rate=self.rate)
        buf = io.BytesIO()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                buf.write(chunk["data"])
        return buf.getvalue()

    @staticmethod
    def _estimate_duration(audio_data: bytes) -> float:
        """
        估算 MP3 音频播放时长（秒）。
        优先使用 pydub 精确解析，失败则按 ~24kbps 估算。
        """
        try:
            from pydub import AudioSegment
            seg = AudioSegment.from_mp3(io.BytesIO(audio_data))
            return len(seg) / 1000.0
        except Exception:
            return max(1.0, len(audio_data) * 8 / 24000)
