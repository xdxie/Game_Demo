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
                                                           on_utterance(text, generation)
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

        self.on_utterance: Optional[Callable[[str, int], None]] = None
        self.on_state_change: Optional[Callable[[str], None]] = None

        self._muted = False
        self._activity_state = "listening"
        self._last_emitted_state = ""
        self._transcription_inflight = 0
        self._gen_inflight = 0
        self._seek_generation = 0
        self._state_lock = threading.RLock()

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

    @property
    def seek_generation(self) -> int:
        with self._state_lock:
            return self._seek_generation

    # ── TTS 联动接口 ──────────────────────────────────────────────────

    def mute(self):
        """TTSQueue 开始播报时调用"""
        with self._state_lock:
            self._cancel_unmute_timer()
            self._muted = True
            self._reset_vad_unlocked()
            if self._activity_state == "recording":
                self._activity_state = (
                    "processing" if self._gen_inflight > 0 else "listening"
                )
            self._emit_state_unlocked()
        logger.debug("ASR muted")

    def unmute(self):
        """TTSQueue 播报结束时调用（含 TTS_MUTE_TAIL_SEC 延迟）"""
        with self._state_lock:
            self._cancel_unmute_timer()
            self._unmute_timer = threading.Timer(self.TTS_MUTE_TAIL_SEC, self._do_unmute)
            self._unmute_timer.start()

    def force_unmute(self):
        """视频 seek 时调用，跳过 tail delay 直接 unmute"""
        with self._state_lock:
            self._cancel_unmute_timer()
            self._muted = False
            self._sync_activity_after_unmute()
            self._emit_state_unlocked()
        logger.debug("ASR force unmuted")

    def reset_for_seek(self):
        """
        视频 seek 时调用：丢弃队列中未处理的音频，并使进行中的转写结果失效。
        防止旧时间点的识别结果在 seek 后触发 USER_QUESTION。
        """
        with self._state_lock:
            self._seek_generation += 1
            drained = 0
            while True:
                try:
                    self._transcription_queue.get_nowait()
                    drained += 1
                except queue.Empty:
                    break
            if drained:
                self._transcription_inflight = max(
                    0, self._transcription_inflight - drained
                )
            self._gen_inflight = 0
            self._reset_vad_unlocked()
            self._activity_state = "listening"
            self._emit_state_unlocked()
        logger.debug(
            "ASR reset for seek (gen=%d, drained=%d)",
            self.seek_generation, drained,
        )

    def _do_unmute(self):
        with self._state_lock:
            self._unmute_timer = None
            self._muted = False
            self._sync_activity_after_unmute()
            self._emit_state_unlocked()
        logger.debug("ASR unmuted")

    def _sync_activity_after_unmute(self):
        """TTS 结束后根据当前 generation 的在途转写恢复活动状态"""
        if self._gen_inflight > 0:
            self._activity_state = "processing"
        elif self._activity_state in ("processing", "recording"):
            self._activity_state = "listening"

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
        with self._state_lock:
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
                self._set_activity_unlocked("recording")

            elif self._speaking:
                self._silence_frames += 1
                self._audio_buffer.append(audio_bytes)

                if self._silence_frames >= silence_limit:
                    if self._speech_frames >= speech_min:
                        self._flush_unlocked()
                    else:
                        self._set_activity_unlocked("listening")
                    self._reset_vad_unlocked()

    # ── 内部实现 ──────────────────────────────────────────────────────

    def _set_activity(self, state: str):
        """更新非 mute 维度的活动状态（listening / recording / processing）"""
        with self._state_lock:
            self._set_activity_unlocked(state)

    def _set_activity_unlocked(self, state: str):
        if self._activity_state != state:
            self._activity_state = state
            self._emit_state_unlocked()

    def _emit_state(self):
        with self._state_lock:
            self._emit_state_unlocked()

    def _emit_state_unlocked(self):
        state = "muted" if self._muted else self._activity_state
        if state == self._last_emitted_state:
            return
        self._last_emitted_state = state
        callback = self.on_state_change
        if callback:
            try:
                callback(state)
            except Exception as e:
                logger.error("ASR on_state_change error: %s", e)

    def _flush(self):
        with self._state_lock:
            self._flush_unlocked()

    def _flush_unlocked(self):
        if not self._audio_buffer:
            return

        raw = b"".join(self._audio_buffer)
        arr = (
            np.frombuffer(raw, dtype=np.int16)
            .astype(np.float32) / 32768.0
        )
        logger.debug("ASR queued %.1fs audio for transcription", len(arr) / 16000)

        try:
            gen = self._seek_generation
            self._transcription_queue.put_nowait((arr, gen))
            self._transcription_inflight += 1
            self._gen_inflight += 1
            self._set_activity_unlocked("processing")
        except queue.Full:
            logger.warning("ASR transcription queue full, dropping audio")
            self._set_activity_unlocked("listening")

    def _transcription_loop(self):
        while True:
            item = self._transcription_queue.get()
            if item is None:
                break
            arr, generation = item
            try:
                result = self.model.transcribe(
                    arr,
                    language=self.language,
                    fp16=False,
                )
                text = result["text"].strip()
                logger.info("ASR result: %s", text)
                callback = None
                utterance_gen = generation
                with self._state_lock:
                    if generation != self._seek_generation:
                        logger.debug("ASR result discarded (stale after seek)")
                        text = ""
                    elif text and self.on_utterance:
                        callback = self.on_utterance
                if callback:
                    callback(text, utterance_gen)
            except Exception as e:
                logger.error("Whisper transcribe error: %s", e)
            finally:
                with self._state_lock:
                    self._transcription_inflight = max(
                        0, self._transcription_inflight - 1
                    )
                    if generation == self._seek_generation:
                        self._gen_inflight = max(0, self._gen_inflight - 1)
                        gen_inflight = self._gen_inflight
                    else:
                        gen_inflight = self._gen_inflight
                    muted = self._muted
                if not muted:
                    if gen_inflight > 0:
                        self._set_activity("processing")
                    else:
                        self._set_activity("listening")

    def _reset_vad(self):
        with self._state_lock:
            self._reset_vad_unlocked()

    def _reset_vad_unlocked(self):
        self._speaking       = False
        self._audio_buffer   = []
        self._speech_frames  = 0
        self._silence_frames = 0

    def stop(self):
        """关闭转写线程"""
        self._cancel_unmute_timer()
        self._transcription_queue.put(None)
