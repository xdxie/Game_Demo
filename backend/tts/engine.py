"""
TTS 引擎封装（火山引擎 / edge-tts）。

合成完成后通过 on_audio_data 回调将完整 MP3 bytes 传出，
由 TTSQueue 带 utterance_id 广播给前端（先合成再 mute，避免误打断）。

引擎选择（config.tts_engine）：
- "volcengine" — 火山引擎 seed-tts-2.0，国内服务器，首包较快
- "edge-tts"   — 微软 Azure，免费无需 key
"""

from __future__ import annotations
import asyncio
import base64
import io
import json
import logging
import threading
from typing import Callable, Optional

import edge_tts
import requests

logger = logging.getLogger(__name__)

PRELOAD_TEXTS = ["向左闪！", "注意，快闪！", "有机会，打！", "进攻！", "注意！"]


class TTSEngine:
    """
    多后端 TTS 封装。
    speak_async 合成完整 MP3 后一次性调用 on_audio_data，与 TTSQueue 协议一致。
    """

    def __init__(
        self,
        engine: str = "edge-tts",
        voice: str = "zh-CN-YunxiNeural",
        rate: str = "+20%",
        synthesis_timeout: float = 15.0,
        volc_api_key: str = "",
        volc_speaker: str = "zh_female_vv_uranus_bigtts",
        volc_speed_ratio: float = 1.5,
    ):
        self.engine = (engine or "edge-tts").strip().lower()
        self.voice = voice
        self.rate = rate
        self._synthesis_timeout = synthesis_timeout

        self._volc_api_key = volc_api_key
        self._volc_speaker = volc_speaker
        self._volc_speed_ratio = volc_speed_ratio
        self._volc_url = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"

        self._stop_flag = threading.Event()
        self._cache: dict[str, bytes] = {}
        self.on_audio_data: Optional[Callable[[bytes], None]] = None

        logger.info("TTS engine: %s", self.engine)

    def preload(self, texts: list[str] | None = None):
        """预合成常用短语（同步入口；在已有事件循环中请用 preload_async）"""
        try:
            asyncio.run(self.preload_async(texts))
        except RuntimeError:
            logger.warning(
                "TTS preload skipped (event loop running); call preload_async instead"
            )

    async def preload_async(self, texts: list[str] | None = None):
        """在 async 上下文中预合成常用短语，存入内存缓存"""
        targets = texts or PRELOAD_TEXTS
        for text in targets:
            if self.engine == "volcengine":
                try:
                    data = await asyncio.get_running_loop().run_in_executor(
                        None, self._synthesize_full_volc, text,
                    )
                    if data:
                        self._cache[text] = data
                except Exception as e:
                    logger.warning("Preload failed for '%s': %s", text, e)
            else:
                await self._async_preload(text)
        logger.info("TTS preloaded %d phrases", len(self._cache))

    async def _async_preload(self, text: str):
        try:
            data = await self._synthesize_edge(text)
            self._cache[text] = data
        except Exception as e:
            logger.warning("Preload failed for '%s': %s", text, e)

    def speak_async(
        self,
        text: str,
        is_cancelled: Optional[Callable[[], bool]] = None,
        on_dispatched: Optional[Callable[[float], None]] = None,
        on_error: Optional[Callable[[], None]] = None,
    ):
        """
        异步合成并发送（在新线程中执行，不阻塞调用方）。
        合成前后均检查 is_cancelled，防止打断后仍发出旧音频。
        """
        self._stop_flag.clear()
        threading.Thread(
            target=self._speak_thread,
            args=(text, is_cancelled, on_dispatched, on_error),
            daemon=True,
            name="tts-speak",
        ).start()

    def stop(self):
        """立即中止进行中的合成"""
        self._stop_flag.set()

    # ── 内部实现 ──────────────────────────────────────────────────────

    def _speak_thread(
        self,
        text: str,
        is_cancelled: Optional[Callable[[], bool]],
        on_dispatched: Optional[Callable[[float], None]],
        on_error: Optional[Callable[[], None]],
    ):
        def _cancelled() -> bool:
            return bool(self._stop_flag.is_set() or (is_cancelled and is_cancelled()))

        loop = None
        audio_data = None
        try:
            if text in self._cache:
                audio_data = self._cache[text]
            elif self.engine == "volcengine":
                audio_data = self._synthesize_streaming_volc(text)
            else:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                audio_data = loop.run_until_complete(
                    asyncio.wait_for(
                        self._synthesize_edge(text),
                        timeout=self._synthesis_timeout,
                    )
                )
        except asyncio.TimeoutError:
            logger.error(
                "TTS synthesis timeout (%.0fs): '%s'",
                self._synthesis_timeout, text[:20],
            )
            if on_error and not _cancelled():
                on_error()
            return
        except Exception as e:
            if _cancelled():
                logger.debug("TTS synthesis cancelled: '%s'", text[:20])
                return
            logger.error("TTS synthesis error: %s", e)
            if on_error and not _cancelled():
                on_error()
            return
        finally:
            if loop is not None:
                try:
                    loop.close()
                except Exception:
                    pass

        if _cancelled():
            logger.debug("TTS synthesis discarded (cancelled): '%s'", text[:20])
            return

        if not audio_data:
            if on_error and not _cancelled():
                on_error()
            return

        if self.on_audio_data:
            try:
                self.on_audio_data(audio_data)
            except Exception as e:
                logger.error("TTS on_audio_data callback error: %s", e)

        if _cancelled():
            logger.debug("TTS dispatch discarded (cancelled): '%s'", text[:20])
            return

        if on_dispatched:
            duration = self._estimate_duration(audio_data)
            logger.debug(
                "TTS dispatched, estimated duration: %.2fs for '%s'",
                duration, text[:20],
            )
            try:
                on_dispatched(duration)
            except Exception as e:
                logger.error("TTS on_dispatched callback error: %s", e)

    async def _synthesize_cached(self, text: str) -> bytes:
        if text in self._cache:
            return self._cache[text]
        if self.engine == "volcengine":
            data = await asyncio.get_running_loop().run_in_executor(
                None, self._synthesize_full_volc, text,
            )
        else:
            data = await self._synthesize_edge(text)
        self._cache[text] = data
        return data

    async def _synthesize_edge(self, text: str) -> bytes:
        """edge-tts 合成完整 MP3"""
        communicate = edge_tts.Communicate(text, voice=self.voice, rate=self.rate)
        buf = io.BytesIO()
        async for chunk in communicate.stream():
            if self._stop_flag.is_set():
                break
            if chunk["type"] == "audio":
                buf.write(chunk["data"])
        return buf.getvalue()

    # ── 火山引擎 ─────────────────────────────────────────────────────

    def _volc_headers(self) -> dict:
        return {
            "X-Api-Key": self._volc_api_key,
            "X-Api-Resource-Id": "seed-tts-2.0",
            "Content-Type": "application/json",
            "Connection": "keep-alive",
        }

    def _volc_payload(self, text: str) -> dict:
        additions = json.dumps({
            "disable_markdown_filter": False,
            "disable_emoji_filter": False,
            "enable_latex_tn": True,
            "context_texts": ["请用急促紧张的语气快速说"],
        })
        return {
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

    def _synthesize_streaming_volc(self, text: str) -> bytes:
        """火山引擎流式合成，内部拼接为完整 MP3 后返回"""
        if not self._volc_api_key:
            logger.error("Volcengine TTS: VOLC_API_KEY not configured")
            return b""

        buf = io.BytesIO()
        session = requests.Session()
        try:
            resp = session.post(
                self._volc_url,
                headers=self._volc_headers(),
                json=self._volc_payload(text),
                stream=True,
                timeout=(5, 30),
            )
            if resp.status_code != 200:
                logger.error(
                    "Volcengine TTS HTTP %d: %s",
                    resp.status_code, resp.text[:200],
                )
                return b""
            for line in resp.iter_lines(decode_unicode=True):
                if self._stop_flag.is_set():
                    break
                if not line:
                    continue
                data = json.loads(line)
                code = data.get("code", 0)
                if code == 0 and data.get("data"):
                    buf.write(base64.b64decode(data["data"]))
                elif code == 20000000:
                    break
                elif code > 0:
                    logger.error(
                        "Volcengine TTS error: code=%d msg=%s",
                        code, data.get("message", ""),
                    )
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
        """火山引擎非流式合成（用于 preload）"""
        return self._synthesize_streaming_volc(text)

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
