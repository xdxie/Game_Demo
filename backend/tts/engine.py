"""
TTS 引擎封装（edge-tts）。

edge-tts 特点：
- 需要联网（调用 Microsoft Edge TTS API）
- 中文语音质量好，延迟约 300-500ms
- 支持 rate/pitch/volume 参数调整

声音调优（3号）：
- 可用中文声音（部分）：
    zh-CN-YunxiNeural    男声，自然
    zh-CN-YunyangNeural  男声，新闻播报风
    zh-CN-XiaoxiaoNeural 女声，自然活泼
    zh-CN-XiaohanNeural  女声，沉稳
- rate 参数："+20%" 到 "+40%" 适合游戏场景（紧迫感）
- 完整声音列表：python -m edge_tts --list-voices | grep zh-CN
"""

from __future__ import annotations
import asyncio
import io
import logging
import threading
from typing import Callable, Optional

import edge_tts

logger = logging.getLogger(__name__)

# 常用短提示词预缓存（避免首次合成延迟）
PRELOAD_TEXTS = ["向左闪！", "注意，快闪！", "有机会，打！", "进攻！", "注意！"]


class TTSEngine:
    """
    edge-tts 封装。支持：
    - 异步合成（speak_async）
    - 同步停止当前播放（stop）
    - 预缓存常用短语
    """

    def __init__(self, voice: str = "zh-CN-YunxiNeural", rate: str = "+20%"):
        """
        Args:
            voice: edge-tts 声音名称（3号调优）
            rate:  语速，"+20%" 表示加速 20%（3号调优）
        """
        self.voice = voice
        self.rate  = rate

        self._current_process: Optional[threading.Thread] = None
        self._stop_flag = threading.Event()
        self._cache: dict[str, bytes] = {}

    def preload(self, texts: list[str] | None = None):
        """预合成常用短语，存入内存缓存"""
        targets = texts or PRELOAD_TEXTS
        for text in targets:
            asyncio.run(self._async_preload(text))
        logger.info("TTS preloaded %d phrases", len(self._cache))

    async def _async_preload(self, text: str):
        try:
            data = await self._synthesize(text)
            self._cache[text] = data
        except Exception as e:
            logger.warning("Preload failed for '%s': %s", text, e)

    def speak_async(self, text: str, on_complete: Optional[Callable] = None):
        """
        异步播放文本（在新线程中执行，不阻塞调用方）。
        on_complete: 播放完成后回调（在工作线程中执行）
        """
        self._stop_flag.clear()
        self._current_process = threading.Thread(
            target=self._speak_thread,
            args=(text, on_complete),
            daemon=True,
        )
        self._current_process.start()

    def stop(self):
        """立即停止当前播放"""
        self._stop_flag.set()

    # ── 内部实现 ──────────────────────────────────────────────────────

    def _speak_thread(self, text: str, on_complete: Optional[Callable]):
        """在独立线程中执行合成 + 播放"""
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self._async_speak(text))
        except Exception as e:
            logger.error("TTS speak error: %s", e)
        finally:
            if on_complete and not self._stop_flag.is_set():
                on_complete()

    async def _async_speak(self, text: str):
        """合成 + 播放（优先走缓存）"""
        if self._stop_flag.is_set():
            return

        # 走缓存
        audio_data = self._cache.get(text)
        if audio_data is None:
            audio_data = await self._synthesize(text)

        if self._stop_flag.is_set():
            return

        await self._play_audio(audio_data)

    async def _synthesize(self, text: str) -> bytes:
        """调用 edge-tts API 合成音频，返回 bytes"""
        communicate = edge_tts.Communicate(text, voice=self.voice, rate=self.rate)
        buf = io.BytesIO()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                buf.write(chunk["data"])
        return buf.getvalue()

    async def _play_audio(self, audio_data: bytes):
        """播放 MP3 音频数据"""
        try:
            import pygame
            if not pygame.mixer.get_init():
                pygame.mixer.init(frequency=22050, size=-16, channels=1, buffer=512)
            buf = io.BytesIO(audio_data)
            pygame.mixer.music.load(buf)
            pygame.mixer.music.play()
            # 等待播放完成或被停止
            while pygame.mixer.music.get_busy():
                if self._stop_flag.is_set():
                    pygame.mixer.music.stop()
                    return
                await asyncio.sleep(0.05)
        except ImportError:
            # fallback: pyaudio
            await self._play_with_pyaudio(audio_data)

    async def _play_with_pyaudio(self, audio_data: bytes):
        """使用 pyaudio 播放（fallback）"""
        import pyaudio
        import struct

        # edge-tts 返回 MP3，需要解码
        try:
            import pydub
            segment = pydub.AudioSegment.from_mp3(io.BytesIO(audio_data))
            raw = segment.raw_data
            channels   = segment.channels
            sampwidth  = segment.sample_width
            framerate  = segment.frame_rate
        except ImportError:
            logger.warning("pydub not installed, skipping audio playback")
            return

        pa = pyaudio.PyAudio()
        stream = pa.open(
            format=pa.get_format_from_width(sampwidth),
            channels=channels,
            rate=framerate,
            output=True,
        )
        chunk_size = 1024
        for i in range(0, len(raw), chunk_size):
            if self._stop_flag.is_set():
                break
            stream.write(raw[i:i + chunk_size])
        stream.stop_stream()
        stream.close()
        pa.terminate()
