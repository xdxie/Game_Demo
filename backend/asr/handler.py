"""
ASR 持续收音处理器：Whisper + VAD，自动识别用户提问。
TTS 播报期间暂停 VAD，避免回声误触发。

VAD 参数调优（5号）：
- SILENCE_THRESHOLD：在真实环境下测量背景噪声振幅，设置在噪声均值 * 2 左右
- SILENCE_END_SEC：1.2 秒是否合适，正常说话停顿会不会被误判为结束
- TTS_MUTE_TAIL_SEC：0.2 秒是否足够消除尾音
- 安静环境 vs 有背景音（游戏声）分别测试
"""

from __future__ import annotations
import logging
import threading
from typing import Callable, Optional

import numpy as np
import whisper

logger = logging.getLogger(__name__)


class ASRHandler:
    """
    持续收音 + VAD + Whisper 识别。

    使用方式：
    1. 前端通过 WebSocket 持续发送 PCM 音频块（约 100ms/块，16bit 16kHz）
    2. 后端调用 process_audio_chunk() 处理每个音频块
    3. VAD 检测到语音结束后自动调用 on_utterance(text)
    """

    # VAD 参数（5号调优）
    SILENCE_THRESHOLD  = 300    # 振幅阈值（0~32768），需在真实环境下校准
    SPEECH_MIN_SEC     = 0.5    # 最短有效语音，过滤误触（清嗓子、轻微背景音）
    SILENCE_END_SEC    = 1.2    # 静音多久判定说话结束（秒）
    TTS_MUTE_TAIL_SEC  = 0.2   # TTS 结束后额外静默时间（消余音，5号调优）

    def __init__(self, model_size: str = "base", language: str = "zh"):
        """
        Args:
            model_size: Whisper 模型大小（"base" 约 74M，平衡速度和准确率）
                        如准确率不足，可升级到 "small" 或 "medium"（5号评估）
            language:   识别语言
        """
        logger.info("Loading Whisper model: %s ...", model_size)
        self.model    = whisper.load_model(model_size)
        self.language = language
        logger.info("Whisper loaded")

        # 识别完成回调：callable(text: str)
        self.on_utterance: Optional[Callable[[str], None]] = None

        # TTS 联动状态
        self._muted = False

        # VAD 状态
        self._speaking      = False
        self._audio_buffer: list[bytes] = []
        self._speech_frames  = 0
        self._silence_frames = 0

    # ── TTS 联动接口 ──────────────────────────────────────────────────

    def mute(self):
        """TTSQueue 开始播报时调用"""
        self._muted = True
        self._reset_vad()
        logger.debug("ASR muted")

    def unmute(self):
        """TTSQueue 播报结束时调用（含 TTS_MUTE_TAIL_SEC 延迟）"""
        threading.Timer(self.TTS_MUTE_TAIL_SEC, self._do_unmute).start()

    def force_unmute(self):
        """视频 seek 时调用，跳过 tail delay 直接 unmute"""
        self._muted = False
        logger.debug("ASR force unmuted")

    def _do_unmute(self):
        self._muted = False
        logger.debug("ASR unmuted")

    # ── 音频处理接口 ──────────────────────────────────────────────────

    def process_audio_chunk(self, audio_bytes: bytes, sample_rate: int = 16000):
        """
        处理前端发来的 PCM 音频块（WebSocket binary frame）。
        约 100ms/块（1600 samples @ 16kHz）。

        Args:
            audio_bytes: PCM 16bit little-endian
            sample_rate: 采样率（默认 16kHz，需与前端一致）
        """
        if self._muted:
            return

        audio = np.frombuffer(audio_bytes, dtype=np.int16)
        if len(audio) == 0:
            return

        amplitude = float(np.abs(audio).mean())

        # 计算每秒多少个 chunk
        chunks_per_sec = sample_rate / len(audio)
        silence_limit  = int(self.SILENCE_END_SEC * chunks_per_sec)
        speech_min     = int(self.SPEECH_MIN_SEC  * chunks_per_sec)

        if amplitude > self.SILENCE_THRESHOLD:
            # 有声音
            self._speaking = True
            self._silence_frames = 0
            self._speech_frames += 1
            self._audio_buffer.append(audio_bytes)

        elif self._speaking:
            # 静音中（曾经在说话）
            self._silence_frames += 1
            self._audio_buffer.append(audio_bytes)  # 保留静音段（自然停顿）

            if self._silence_frames >= silence_limit:
                # 静音超过阈值，判定说话结束
                if self._speech_frames >= speech_min:
                    self._flush()
                self._reset_vad()

    def _flush(self):
        """将缓冲区音频送入 Whisper 识别"""
        if not self._audio_buffer:
            return

        raw = b"".join(self._audio_buffer)
        arr = (
            np.frombuffer(raw, dtype=np.int16)
            .astype(np.float32) / 32768.0
        )

        logger.debug("Whisper transcribing %.1fs audio...", len(arr) / 16000)

        try:
            result = self.model.transcribe(
                arr,
                language=self.language,
                fp16=False,
            )
            text = result["text"].strip()
            logger.info("ASR result: %s", text)
            if text and self.on_utterance:
                self.on_utterance(text)
        except Exception as e:
            logger.error("Whisper transcribe error: %s", e)

    def _reset_vad(self):
        self._speaking      = False
        self._audio_buffer  = []
        self._speech_frames  = 0
        self._silence_frames = 0
