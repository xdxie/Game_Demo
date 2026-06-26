"""
ASR 持续收音处理器：Whisper + VAD，自动识别用户提问。
TTS 播报期间暂停 VAD，避免回声误触发。

Fix 13：Whisper 识别改为独立线程池，不再阻塞 VAD。
Utterance 握手：通过 on_state_change 向前端广播 listening/recording/processing/muted。
"""

from __future__ import annotations
import logging
import queue
import threading
from typing import Callable, Optional

import numpy as np
import whisper

logger = logging.getLogger(__name__)


class ASRHandler:
    """
    持续收音 + VAD + 非阻塞 Whisper 识别。

    架构：
      WebSocket 线程 → process_audio_chunk() → VAD
                                                  ↓ (语音结束)
                                             _transcription_queue
                                                  ↓
                                     _transcription_thread → whisper.transcribe()
                                                                   ↓
                                                           on_utterance(text)
    """

    def __init__(
        self,
        model_size: str = "base",
        language: str = "zh",
        vad_silence_threshold: int = 300,
        vad_speech_min_sec: float = 0.5,
        vad_silence_end_sec: float = 1.2,
        tts_mute_tail_sec: float = 0.2,
    ):
        self.SILENCE_THRESHOLD = vad_silence_threshold
        self.SPEECH_MIN_SEC    = vad_speech_min_sec
        self.SILENCE_END_SEC   = vad_silence_end_sec
        self.TTS_MUTE_TAIL_SEC = tts_mute_tail_sec

        logger.info("Loading Whisper model: %s ...", model_size)
        self.model    = whisper.load_model(model_size)
        self.language = language
        logger.info("Whisper loaded")

        self.on_utterance: Optional[Callable[[str], None]] = None
        self.on_state_change: Optional[Callable[[str], None]] = None

        self._muted = False
        self._activity_state = "listening"
        self._last_emitted_state = ""

        self._speaking       = False
        self._audio_buffer:  list[bytes] = []
        self._speech_frames  = 0
        self._silence_frames = 0

        self._unmute_timer: Optional[threading.Timer] = None

        self._transcription_queue: queue.Queue = queue.Queue(maxsize=4)
        self._transcription_thread = threading.Thread(
            target=self._transcription_loop,
            daemon=True,
            name="asr-transcribe",
        )
        self._transcription_thread.start()

        self._emit_state()

    # ── TTS 联动接口 ──────────────────────────────────────────────────

    def mute(self):
        """TTSQueue 开始播报时调用"""
        self._cancel_unmute_timer()
        self._muted = True
        self._reset_vad()
        self._emit_state()
        logger.debug("ASR muted")

    def unmute(self):
        """TTSQueue 播报结束时调用（含 TTS_MUTE_TAIL_SEC 延迟）"""
        self._cancel_unmute_timer()
        self._unmute_timer = threading.Timer(self.TTS_MUTE_TAIL_SEC, self._do_unmute)
        self._unmute_timer.start()

    def force_unmute(self):
        """视频 seek 时调用，跳过 tail delay 直接 unmute"""
        self._cancel_unmute_timer()
        self._muted = False
        self._emit_state()
        logger.debug("ASR force unmuted")

    def _do_unmute(self):
        self._unmute_timer = None
        self._muted = False
        self._emit_state()
        logger.debug("ASR unmuted")

    def _cancel_unmute_timer(self):
        if self._unmute_timer is not None:
            self._unmute_timer.cancel()
            self._unmute_timer = None

    # ── 音频处理接口 ──────────────────────────────────────────────────

    def process_audio_chunk(self, audio_bytes: bytes, sample_rate: int = 16000):
        """
        处理前端发来的 PCM 音频块（WebSocket binary frame）。
        约 100ms/块（1600 samples @ 16kHz）。
        """
        if self._muted:
            return

        audio = np.frombuffer(audio_bytes, dtype=np.int16)
        if len(audio) == 0:
            return

        amplitude = float(np.abs(audio).mean())

        chunks_per_sec = sample_rate / len(audio)
        silence_limit  = int(self.SILENCE_END_SEC * chunks_per_sec)
        speech_min     = int(self.SPEECH_MIN_SEC  * chunks_per_sec)

        if amplitude > self.SILENCE_THRESHOLD:
            self._speaking = True
            self._silence_frames = 0
            self._speech_frames += 1
            self._audio_buffer.append(audio_bytes)
            self._set_activity("recording")

        elif self._speaking:
            self._silence_frames += 1
            self._audio_buffer.append(audio_bytes)

            if self._silence_frames >= silence_limit:
                if self._speech_frames >= speech_min:
                    self._flush()
                self._reset_vad()
                self._set_activity("listening")

    # ── 内部实现 ──────────────────────────────────────────────────────

    def _set_activity(self, state: str):
        """更新非 mute 维度的活动状态（listening / recording / processing）"""
        if self._activity_state != state:
            self._activity_state = state
            self._emit_state()

    def _emit_state(self):
        state = "muted" if self._muted else self._activity_state
        if state == self._last_emitted_state:
            return
        self._last_emitted_state = state
        if self.on_state_change:
            try:
                self.on_state_change(state)
            except Exception as e:
                logger.error("ASR on_state_change error: %s", e)

    def _flush(self):
        if not self._audio_buffer:
            return

        raw = b"".join(self._audio_buffer)
        arr = (
            np.frombuffer(raw, dtype=np.int16)
            .astype(np.float32) / 32768.0
        )
        logger.debug("ASR queued %.1fs audio for transcription", len(arr) / 16000)

        try:
            self._transcription_queue.put_nowait(arr)
            self._set_activity("processing")
        except queue.Full:
            logger.warning("ASR transcription queue full, dropping audio")
            self._set_activity("listening")

    def _transcription_loop(self):
        while True:
            arr = self._transcription_queue.get()
            if arr is None:
                break
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
            finally:
                if not self._muted:
                    self._set_activity("listening")

    def _reset_vad(self):
        self._speaking       = False
        self._audio_buffer   = []
        self._speech_frames  = 0
        self._silence_frames = 0

    def stop(self):
        """关闭转写线程"""
        self._cancel_unmute_timer()
        self._transcription_queue.put(None)
