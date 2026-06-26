"""
TTS 引擎封装（支持火山引擎 / edge-tts）。

合成完成后通过 on_audio_data 回调将 MP3 bytes 传出，
由 TTSQueue 广播给所有 WebSocket 客户端。

支持流式合成：每收到一个音频 chunk 就立即调用 on_audio_data，
前端可边收边播，首包延迟从 2-5 秒降低到几百毫秒。

引擎选择（config.py 中 tts_engine 配置）：
- "volcengine" — 火山引擎 seed-tts-2.0，国内服务器，首包 ~600ms
- "edge-tts"   — 微软 Azure，免费无需 key，首包 ~1s

声音调优（3号）：
- 火山引擎音色列表：控制台 → 语音技术 → 语音合成
- edge-tts 声音列表：python -m edge_tts --list-voices | findstr zh-CN
"""

from __future__ import annotations
import asyncio
import base64
import io
import json
import logging
import threading
import uuid
from typing import Callable, Optional

import edge_tts
import requests

logger = logging.getLogger(__name__)

PRELOAD_TEXTS = ["向左闪！", "注意，快闪！", "有机会，打！", "进攻！", "注意！"]


class TTSEngine:
    """
    多后端 TTS 流式封装。
    流式模式：每个音频 chunk 到达时立即调用 on_audio_data。
    """

    def __init__(
        self,
        engine: str = "volcengine",
        voice: str = "zh-CN-YunxiNeural",
        rate: str = "+20%",
        volc_api_key: str = "",
        volc_speaker: str = "zh_female_vv_uranus_bigtts",
        volc_speed_ratio: float = 1.5,
    ):
        self.engine = engine
        self.voice = voice
        self.rate = rate

        self._volc_api_key = volc_api_key
        self._volc_speaker = volc_speaker
        self._volc_speed_ratio = volc_speed_ratio
        self._volc_url = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"

        self._stop_flag = threading.Event()
        self._completion_timer: Optional[threading.Timer] = None
        self._cache: dict[str, bytes] = {}

        self.on_audio_data: Optional[Callable[[bytes], None]] = None

        logger.info("TTS engine: %s", engine)

    def preload(self, texts: list[str] | None = None):
        """预合成常用短语，存入内存缓存"""
        targets = texts or PRELOAD_TEXTS
        for text in targets:
            try:
                if self.engine == "volcengine":
                    data = self._synthesize_full_volc(text)
                else:
                    asyncio.run(self._async_preload(text))
                    continue
                if data:
                    self._cache[text] = data
            except Exception as e:
                logger.warning("Preload failed for '%s': %s", text, e)
        logger.info("TTS preloaded %d phrases", len(self._cache))

    async def _async_preload(self, text: str):
        try:
            data = await self._synthesize_full(text)
            self._cache[text] = data
        except Exception as e:
            logger.warning("Preload failed for '%s': %s", text, e)

    def speak_async(self, text: str, on_complete: Optional[Callable] = None):
        """异步合成并发送，不阻塞调用方。"""
        self._stop_flag.clear()
        if self._completion_timer:
            self._completion_timer.cancel()
            self._completion_timer = None

        threading.Thread(
            target=self._speak_thread,
            args=(text, on_complete),
            daemon=True,
            name="tts-speak",
        ).start()

    def stop(self):
        """立即中止"""
        self._stop_flag.set()
        if self._completion_timer:
            self._completion_timer.cancel()
            self._completion_timer = None

    # ── 内部实现 ──────────────────────────────────────────────────────

    def _speak_thread(self, text: str, on_complete: Optional[Callable]):
        try:
            if text in self._cache:
                audio_data = self._cache[text]
                if self.on_audio_data:
                    self.on_audio_data(audio_data)
            elif self.engine == "volcengine":
                audio_data = self._synthesize_streaming_volc(text)
            else:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                audio_data = loop.run_until_complete(
                    self._synthesize_streaming(text)
                )
        except Exception as e:
            logger.error("TTS synthesis error: %s", e)
            if on_complete:
                on_complete()
            return

        if self._stop_flag.is_set():
            return

        if not audio_data:
            if on_complete:
                on_complete()
            return

        if on_complete:
            duration = self._estimate_duration(audio_data)
            logger.debug("TTS estimated duration: %.2fs for '%s'", duration, text[:20])
            self._completion_timer = threading.Timer(duration, on_complete)
            self._completion_timer.start()

    # ── 火山引擎 ─────────────────────────────────────────────────────

    def _synthesize_streaming_volc(self, text: str) -> bytes:
        """火山引擎 V3 流式合成：每个 chunk 立即调用 on_audio_data"""
        headers = {
            "X-Api-Key": self._volc_api_key,
            "X-Api-Resource-Id": "seed-tts-2.0",
            "Content-Type": "application/json",
            "Connection": "keep-alive",
        }
        additions = json.dumps({
            "disable_markdown_filter": False,
            "disable_emoji_filter": False,
            "enable_latex_tn": True,
            "context_texts": ["请用急促紧张的语气快速说"],
        })
        payload = {
            "req_params": {
                "text": text,
                "speaker": self._volc_speaker,
                "additions": additions,
                "audio_params": {
                    "format": "mp3",
                    "sample_rate": 24000,
                    "speed_ratio": self._volc_speed_ratio,
                },
            }
        }

        buf = io.BytesIO()
        session = requests.Session()
        try:
            resp = session.post(
                self._volc_url, headers=headers, json=payload,
                stream=True, timeout=(5, 30),
            )
            if resp.status_code != 200:
                logger.error("Volcengine TTS HTTP %d: %s", resp.status_code, resp.text[:200])
                return b""
            for line in resp.iter_lines(decode_unicode=True):
                if self._stop_flag.is_set():
                    break
                if not line:
                    continue
                data = json.loads(line)
                code = data.get("code", 0)
                if code == 0 and data.get("data"):
                    chunk = base64.b64decode(data["data"])
                    buf.write(chunk)
                    if self.on_audio_data:
                        self.on_audio_data(chunk)
                elif code == 20000000:
                    break
                elif code > 0:
                    logger.error("Volcengine TTS error: code=%d msg=%s", code, data.get("message", ""))
                    break
            resp.close()
        except Exception as e:
            logger.error("Volcengine TTS request failed: %s", e)
        finally:
            session.close()

        full = buf.getvalue()
        if full:
            self._cache[text] = full
        return full

    def _synthesize_full_volc(self, text: str) -> bytes:
        """火山引擎非流式合成（用于 preload），不调 on_audio_data"""
        headers = {
            "X-Api-Key": self._volc_api_key,
            "X-Api-Resource-Id": "seed-tts-2.0",
            "Content-Type": "application/json",
        }
        additions = json.dumps({
            "disable_markdown_filter": False,
            "disable_emoji_filter": False,
            "enable_latex_tn": True,
            "context_texts": ["请用急促紧张的语气快速说"],
        })
        payload = {
            "req_params": {
                "text": text,
                "speaker": self._volc_speaker,
                "additions": additions,
                "audio_params": {
                    "format": "mp3",
                    "sample_rate": 24000,
                    "speed_ratio": self._volc_speed_ratio,
                },
            }
        }

        buf = io.BytesIO()
        try:
            resp = requests.post(
                self._volc_url, headers=headers, json=payload,
                stream=True, timeout=(5, 30),
            )
            for line in resp.iter_lines(decode_unicode=True):
                if not line:
                    continue
                data = json.loads(line)
                code = data.get("code", 0)
                if code == 0 and data.get("data"):
                    buf.write(base64.b64decode(data["data"]))
                elif code == 20000000:
                    break
                elif code > 0:
                    break
            resp.close()
        except Exception as e:
            logger.warning("Volcengine preload failed for '%s': %s", text, e)
        return buf.getvalue()

    # ── edge-tts ─────────────────────────────────────────────────────

    async def _synthesize_streaming(self, text: str) -> bytes:
        """edge-tts 流式合成：每个 chunk 立即调用 on_audio_data，同时拼接完整数据用于缓存"""
        communicate = edge_tts.Communicate(text, voice=self.voice, rate=self.rate)
        buf = io.BytesIO()
        async for chunk in communicate.stream():
            if self._stop_flag.is_set():
                break
            if chunk["type"] == "audio":
                data = chunk["data"]
                buf.write(data)
                if self.on_audio_data:
                    self.on_audio_data(data)
        full = buf.getvalue()
        self._cache[text] = full
        return full

    async def _synthesize_full(self, text: str) -> bytes:
        """edge-tts 非流式合成（用于 preload），返回完整 MP3 bytes"""
        communicate = edge_tts.Communicate(text, voice=self.voice, rate=self.rate)
        buf = io.BytesIO()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                buf.write(chunk["data"])
        return buf.getvalue()

    @staticmethod
    def _estimate_duration(audio_data: bytes) -> float:
        """估算 MP3 播放时长（秒）"""
        try:
            from pydub import AudioSegment
            seg = AudioSegment.from_mp3(io.BytesIO(audio_data))
            return len(seg) / 1000.0 + 0.3
        except Exception:
            return max(1.0, len(audio_data) * 8 / 24000) + 0.3
