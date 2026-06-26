"""
ASR 持续收音处理器：Whisper + VAD，自动识别用户提问。
TTS 播报期间暂停 VAD，避免回声误触发。

支持两种引擎（config.asr_engine）：
  - "faster-whisper"：CTranslate2 加速，支持 GPU/CPU，推荐
  - "openai-whisper"：原版，兼容性好

Fix 13：Whisper 识别改为独立线程池，不再阻塞 VAD。
Utterance 握手：通过 on_state_change 向前端广播 listening/recording/processing/muted。
"""

from __future__ import annotations
import logging
import queue
import threading
from typing import Callable, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)


def _load_model(model_size: str, engine: str, device: str) -> Tuple[object, str]:
    """
    根据配置加载 Whisper 模型，返回 (model, engine_type)。
    engine_type 用于 _transcription_loop 区分 API 调用方式。
    """
    engine = (engine or "openai-whisper").strip().lower()
    if engine == "faster-whisper":
        from faster_whisper import WhisperModel

        if device == "auto":
            try:
                import torch
                use_cuda = torch.cuda.is_available()
            except ImportError:
                use_cuda = False
            actual_device = "cuda" if use_cuda else "cpu"
        else:
            actual_device = device

        compute_type = "float16" if actual_device == "cuda" else "int8"
        logger.info(
            "Loading faster-whisper '%s' on %s (%s) ...",
            model_size, actual_device, compute_type,
        )
        model = WhisperModel(model_size, device=actual_device, compute_type=compute_type)
        logger.info("faster-whisper loaded (%s %s)", actual_device, compute_type)
        return model, "faster-whisper"

    import whisper
    if not hasattr(whisper, "load_model"):
        raise ImportError(
            "当前 `whisper` 模块不是 openai-whisper（缺少 load_model）。\n"
            "请执行：pip uninstall whisper -y && pip install openai-whisper"
        )
    logger.info("Loading openai-whisper '%s' ...", model_size)
    model = whisper.load_model(model_size)
    logger.info("openai-whisper loaded")
    return model, "openai-whisper"


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
        engine: str = "openai-whisper",
        device: str = "auto",
        vad_silence_threshold: int = 300,
        vad_speech_min_sec: float = 0.5,
        vad_silence_end_sec: float = 1.2,
        vad_silence_end_short_sec: float = 0.6,
        vad_adaptive_boundary_sec: float = 1.0,
        vad_max_speech_sec: float = 8.0,
        tts_mute_tail_sec: float = 0.2,
        barge_in_enabled: bool = True,
        barge_in_threshold_mult: float = 1.35,
        whisper_model=None,
        asr_engine_type: Optional[str] = None,
    ):
        self.SILENCE_THRESHOLD = vad_silence_threshold
        self.SPEECH_MIN_SEC    = vad_speech_min_sec
        self.SILENCE_END_SEC   = vad_silence_end_sec
        self.SILENCE_END_SHORT = vad_silence_end_short_sec
        self.SILENCE_END_LONG  = vad_silence_end_sec
        self.ADAPTIVE_BOUNDARY = vad_adaptive_boundary_sec
        self.MAX_SPEECH_SEC    = vad_max_speech_sec
        self.TTS_MUTE_TAIL_SEC = tts_mute_tail_sec
        self._barge_in_enabled = barge_in_enabled
        self._barge_in_threshold = int(
            vad_silence_threshold * barge_in_threshold_mult
        )

        if whisper_model is not None:
            self.model = whisper_model
            self._engine_type = asr_engine_type or engine
            logger.info("ASR using pre-warmed model (%s)", self._engine_type)
        else:
            self.model, self._engine_type = _load_model(model_size, engine, device)
        self.language = language

        self.on_utterance: Optional[Callable[[str, int], None]] = None
        self.on_state_change: Optional[Callable[[str], None]] = None
        self.on_barge_in: Optional[Callable[[], bool]] = None
        self.is_tts_playing: Optional[Callable[[], bool]] = None

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
        self._chunk_samples  = 1600

        self._unmute_timer: Optional[threading.Timer] = None

        self._barge_in_frames = 0
        self._barge_in_armed = True

        self._transcription_queue: queue.Queue = queue.Queue(maxsize=4)
        self._transcription_thread = threading.Thread(
            target=self._transcription_loop,
            daemon=True,
            name="asr-transcribe",
        )
        self._transcription_thread.start()

        self._emit_state()

    @property
    def activity_state(self) -> str:
        with self._state_lock:
            return self._activity_state if not self._muted else "muted"

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
            self._barge_in_frames = 0
            self._barge_in_armed = True
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
        """视频 seek 或 barge-in 后调用，跳过 tail delay 直接 unmute"""
        with self._state_lock:
            self._cancel_unmute_timer()
            self._muted = False
            self._barge_in_armed = True
            self._barge_in_frames = 0
            self._reset_vad_unlocked()
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
                playing = bool(
                    self.is_tts_playing and self.is_tts_playing()
                )
                if playing:
                    self._check_barge_in_unlocked(audio_bytes, sample_rate)
                    if self._muted:
                        return
                else:
                    # TTS 已结束但仍 mute（尾音延迟 / 误触发 barge-in）→ 立即恢复
                    self._cancel_unmute_timer()
                    self._muted = False
                    self._barge_in_armed = True
                    self._barge_in_frames = 0
                    self._emit_state_unlocked()

            audio = np.frombuffer(audio_bytes, dtype=np.int16)
            if len(audio) == 0:
                return

            self._chunk_samples = len(audio)
            amplitude = self._chunk_level(audio)

            chunks_per_sec = sample_rate / len(audio)
            speech_min = int(self.SPEECH_MIN_SEC * chunks_per_sec)

            speech_duration = (
                self._speech_frames / chunks_per_sec if chunks_per_sec > 0 else 0
            )
            if speech_duration < self.ADAPTIVE_BOUNDARY:
                silence_end_sec = self.SILENCE_END_SHORT
            else:
                silence_end_sec = self.SILENCE_END_LONG
            silence_limit = int(silence_end_sec * chunks_per_sec)

            if amplitude > self.SILENCE_THRESHOLD:
                self._speaking = True
                self._silence_frames = 0
                self._speech_frames += 1
                self._audio_buffer.append(audio_bytes)
                self._set_activity_unlocked("recording")

            elif self._speaking:
                self._silence_frames += 1
                self._audio_buffer.append(audio_bytes)

                if speech_duration >= self.MAX_SPEECH_SEC:
                    if self._speech_frames >= speech_min:
                        self._flush_unlocked()
                    else:
                        self._set_activity_unlocked("listening")
                    self._reset_vad_unlocked()
                elif self._silence_frames >= silence_limit:
                    if self._speech_frames >= speech_min:
                        self._flush_unlocked()
                    else:
                        self._set_activity_unlocked("listening")
                    self._reset_vad_unlocked()

    def _check_barge_in_unlocked(self, audio_bytes: bytes, sample_rate: int):
        """TTS 播报期间检测用户说话 → 触发打断回调（不写入转写缓冲）。"""
        if not self._barge_in_enabled or not self._barge_in_armed:
            return
        checker = self.is_tts_playing
        if checker is not None and not checker():
            return

        audio = np.frombuffer(audio_bytes, dtype=np.int16)
        if len(audio) == 0:
            return

        amplitude = self._chunk_level(audio)
        chunks_per_sec = sample_rate / len(audio)
        speech_min = int(self.SPEECH_MIN_SEC * chunks_per_sec)

        if amplitude > self._barge_in_threshold:
            self._barge_in_frames += 1
            if self._barge_in_frames >= speech_min:
                self._barge_in_frames = 0
                callback = self.on_barge_in
                handled = False
                if callback:
                    try:
                        handled = bool(callback())
                    except Exception as e:
                        logger.error("ASR on_barge_in error: %s", e)
                if handled:
                    self._barge_in_armed = False
                else:
                    self._barge_in_armed = True
        else:
            self._barge_in_frames = max(0, self._barge_in_frames - 1)

    # ── 内部实现 ──────────────────────────────────────────────────────

    @staticmethod
    def _chunk_level(audio: np.ndarray) -> float:
        """语音块响度：RMS 与峰值加权，比单纯 mean(abs) 更稳。"""
        if audio.size == 0:
            return 0.0
        abs_a = np.abs(audio.astype(np.float64))
        peak = float(abs_a.max())
        rms = float(np.sqrt(np.mean(abs_a ** 2)))
        return max(rms, peak * 0.08)

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
        arr = np.frombuffer(raw, dtype=np.int16)

        chunk_size = max(1, self._chunk_samples)
        n_chunks = len(arr) // chunk_size
        if n_chunks > 0:
            start = 0
            end = n_chunks
            for i in range(n_chunks):
                chunk = arr[i * chunk_size:(i + 1) * chunk_size]
                if self._chunk_level(chunk) > self.SILENCE_THRESHOLD:
                    start = i
                    break
            for i in range(n_chunks - 1, start - 1, -1):
                chunk = arr[i * chunk_size:(i + 1) * chunk_size]
                if self._chunk_level(chunk) > self.SILENCE_THRESHOLD:
                    end = i + 1
                    break
            arr = arr[start * chunk_size:end * chunk_size]

        if len(arr) == 0:
            return

        arr_f32 = arr.astype(np.float32) / 32768.0
        peak = float(np.max(np.abs(arr_f32))) if arr_f32.size else 0.0
        logger.info(
            "ASR queued %.2fs audio for transcription (peak=%.3f)",
            len(arr_f32) / 16000, peak,
        )

        try:
            gen = self._seek_generation
            self._transcription_queue.put_nowait((arr_f32, gen))
            self._transcription_inflight += 1
            self._gen_inflight += 1
            self._set_activity_unlocked("processing")
        except queue.Full:
            logger.warning("ASR transcription queue full, dropping audio")
            self._set_activity_unlocked("listening")

    def _transcribe(self, arr: np.ndarray) -> str:
        duration_sec = len(arr) / 16000.0
        peak = float(np.max(np.abs(arr))) if arr.size else 0.0
        if self._engine_type == "faster-whisper":
            segments, info = self.model.transcribe(
                arr,
                language=self.language,
                condition_on_previous_text=False,
                vad_filter=True,
                no_speech_threshold=0.5,
            )
            text = "".join(s.text for s in segments).strip()
            if not text:
                logger.info(
                    "ASR empty (faster-whisper): %.2fs audio peak=%.4f lang_prob=%.2f",
                    duration_sec,
                    peak,
                    getattr(info, "language_probability", 0.0),
                )
            return text
        result = self.model.transcribe(arr, language=self.language, fp16=False)
        text = result["text"].strip()
        if not text:
            logger.info(
                "ASR empty (openai-whisper): %.2fs audio peak=%.4f",
                duration_sec, peak,
            )
        return text

    def _transcription_loop(self):
        while True:
            item = self._transcription_queue.get()
            if item is None:
                break
            arr, generation = item
            try:
                text = self._transcribe(arr)
                logger.info("ASR result: %r", text)
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
