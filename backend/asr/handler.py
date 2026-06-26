"""
ASR 持续收音处理器：Whisper + VAD，自动识别用户提问。
TTS 播报期间暂停 VAD，避免回声误触发。

支持两种引擎（config.py 中 asr_engine 配置）：
  - "faster-whisper"：CTranslate2 加速，支持 GPU/CPU，推荐
  - "openai-whisper"：原版，兼容性好，无 GPU 依赖问题

Fix 13：Whisper 识别改为独立线程池，不再阻塞 VAD。
_flush() 立即将音频放入队列后重置 VAD，识别在后台线程完成，
期间新的语音仍可继续被 VAD 捕捉。

VAD 参数调优（5号）：
- SILENCE_THRESHOLD：在真实环境下测量背景噪声振幅，设置在噪声均值 * 2 左右
- 自适应 VAD：短句用短静音阈值快速响应，长句用长静音阈值避免截断
- TTS_MUTE_TAIL_SEC：0.2 秒是否足够消除尾音
- 安静环境 vs 有背景音（游戏声）分别测试
"""

from __future__ import annotations
import logging
import queue
import threading
from typing import Callable, Optional

import numpy as np

logger = logging.getLogger(__name__)


def _load_model(model_size: str, engine: str, device: str):
    """
    根据配置加载 Whisper 模型，返回 (model, engine_type)。
    engine_type 用于 _transcription_loop 区分 API 调用方式。
    """
    if engine == "faster-whisper":
        from faster_whisper import WhisperModel

        # 自动检测设备
        if device == "auto":
            try:
                import torch
                use_cuda = torch.cuda.is_available()
            except ImportError:
                use_cuda = False
            actual_device = "cuda" if use_cuda else "cpu"
        else:
            actual_device = device

        if actual_device == "cuda":
            compute_type = "float16"
        else:
            compute_type = "int8"

        logger.info("Loading faster-whisper '%s' on %s (%s) ...",
                     model_size, actual_device, compute_type)
        model = WhisperModel(model_size, device=actual_device, compute_type=compute_type)
        logger.info("faster-whisper loaded (%s %s)", actual_device, compute_type)
        return model, "faster-whisper"

    else:
        import whisper
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
                                                           on_utterance(text)
    """

    # VAD 参数（5号调优）
    SILENCE_THRESHOLD  = 75     # 振幅阈值（0~32768），需在真实环境下校准
    SPEECH_MIN_SEC     = 0.5    # 最短有效语音，过滤误触（清嗓子、轻微背景音）
    TTS_MUTE_TAIL_SEC  = 0.2    # TTS 结束后额外静默时间（消余音）

    # 自适应 VAD：短句用短静音阈值（快速响应），长句用长静音阈值（不截断）
    SILENCE_END_SHORT  = 0.6    # 累积语音 < 1s 时的静音判定
    SILENCE_END_LONG   = 1.0    # 累积语音 >= 1s 时的静音判定
    ADAPTIVE_BOUNDARY  = 1.0    # 短句/长句分界（秒）

    def __init__(
        self,
        model_size: str = "base",
        language: str = "zh",
        engine: str = "faster-whisper",
        device: str = "auto",
        vad_silence_threshold: int = 75,
        vad_speech_min_sec: float = 0.5,
        vad_silence_end_sec: float = 1.0,
        vad_silence_end_short_sec: float = 0.6,
        vad_adaptive_boundary_sec: float = 1.0,
        vad_max_speech_sec: float = 8.0,
        tts_mute_tail_sec: float = 0.2,
        barge_in_enabled: bool = True,
        barge_in_threshold_mult: float = 1.35,
        whisper_model=None,
        asr_engine_type: str | None = None,
    ):
        self.SILENCE_THRESHOLD = vad_silence_threshold
        self.SPEECH_MIN_SEC    = vad_speech_min_sec
        self.SILENCE_END_SHORT = vad_silence_end_short_sec
        self.SILENCE_END_LONG  = vad_silence_end_sec
        self.ADAPTIVE_BOUNDARY = vad_adaptive_boundary_sec
        self.TTS_MUTE_TAIL_SEC = tts_mute_tail_sec

        if whisper_model is not None:
            self.model = whisper_model
            self._engine_type = asr_engine_type or engine
            logger.info("ASR using pre-warmed model (%s)", self._engine_type)
        else:
            self.model, self._engine_type = _load_model(model_size, engine, device)
        self.language = language

        self.on_utterance: Optional[Callable] = None
        self.on_state_change: Optional[Callable[[str], None]] = None
        self.on_barge_in: Optional[Callable[[], bool]] = None
        self.is_tts_playing: Optional[Callable[[], bool]] = None

        self._muted = False
        self._seek_generation = 0

        # VAD 状态
        self._speaking       = False
        self._audio_buffer:  list[bytes] = []
        self._speech_frames  = 0
        self._silence_frames = 0
        self._chunk_samples  = 1600

        self._unmute_timer: Optional[threading.Timer] = None

        # Fix 13：独立转写线程 + 队列
        # 队列中存放 float32 numpy array，None 为停止信号
        self._transcription_queue: queue.Queue = queue.Queue(maxsize=4)
        self._transcription_thread = threading.Thread(
            target=self._transcription_loop,
            daemon=True,
            name="asr-transcribe",
        )
        self._transcription_thread.start()

    # ── TTS 联动接口 ──────────────────────────────────────────────────

    def mute(self):
        """TTSQueue 开始播报时调用"""
        self._muted = True
        self._reset_vad()
        self._emit_state()
        logger.debug("ASR muted")

    def unmute(self):
        """TTSQueue 播报结束时调用（含 TTS_MUTE_TAIL_SEC 延迟）"""
        if self._unmute_timer:
            self._unmute_timer.cancel()
        self._unmute_timer = threading.Timer(self.TTS_MUTE_TAIL_SEC, self._do_unmute)
        self._unmute_timer.start()

    def force_unmute(self):
        """视频 seek 时调用，跳过 tail delay 直接 unmute"""
        if self._unmute_timer:
            self._unmute_timer.cancel()
            self._unmute_timer = None
        self._muted = False
        self._emit_state()
        logger.debug("ASR force unmuted")

    def _do_unmute(self):
        self._muted = False
        self._emit_state()
        logger.debug("ASR unmuted")

    @property
    def seek_generation(self) -> int:
        return self._seek_generation

    @property
    def activity_state(self) -> str:
        if self._muted:
            return "muted"
        if self._speaking:
            return "recording"
        return "listening"

    def _emit_state(self):
        if self.on_state_change:
            self.on_state_change(self.activity_state)

    def reset_for_seek(self):
        """seek 时重置 VAD 状态并递增 generation"""
        self._seek_generation += 1
        self._reset_vad()
        self._muted = False
        self._emit_state()

    # ── 音频处理接口 ──────────────────────────────────────────────────

    def process_audio_chunk(self, audio_bytes: bytes, sample_rate: int = 16000):
        """
        处理前端发来的 PCM 音频块（WebSocket binary frame）。
        约 100ms/块（1600 samples @ 16kHz）。
        本方法在 WebSocket 协程中调用，必须快速返回，不得阻塞。

        Args:
            audio_bytes: PCM 16bit little-endian
            sample_rate: 采样率（默认 16kHz，需与前端一致）
        """
        if self._muted:
            return

        audio = np.frombuffer(audio_bytes, dtype=np.int16)
        if len(audio) == 0:
            return

        self._chunk_samples = len(audio)
        amplitude = float(np.abs(audio).mean())

        chunks_per_sec = sample_rate / len(audio)
        speech_min     = int(self.SPEECH_MIN_SEC  * chunks_per_sec)

        # 自适应静音判定：短句快响应，长句不截断
        speech_duration = self._speech_frames / chunks_per_sec if chunks_per_sec > 0 else 0
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

        elif self._speaking:
            self._silence_frames += 1
            self._audio_buffer.append(audio_bytes)

            if self._silence_frames >= silence_limit:
                if self._speech_frames >= speech_min:
                    self._flush()   # 非阻塞：仅入队
                self._reset_vad()

    # ── 内部实现 ──────────────────────────────────────────────────────

    def _flush(self):
        """
        将缓冲区音频转为 float32 放入转写队列，立即返回。
        送入前裁掉首尾静音帧，减少 Whisper 处理量。
        """
        if not self._audio_buffer:
            return

        raw = b"".join(self._audio_buffer)
        arr = np.frombuffer(raw, dtype=np.int16)

        chunk_size = self._chunk_samples
        n_chunks = len(arr) // chunk_size
        if n_chunks > 0:
            start = 0
            end = n_chunks
            for i in range(n_chunks):
                chunk_amp = float(np.abs(arr[i * chunk_size:(i + 1) * chunk_size]).mean())
                if chunk_amp > self.SILENCE_THRESHOLD:
                    start = i
                    break
            for i in range(n_chunks - 1, start - 1, -1):
                chunk_amp = float(np.abs(arr[i * chunk_size:(i + 1) * chunk_size]).mean())
                if chunk_amp > self.SILENCE_THRESHOLD:
                    end = i + 1
                    break
            arr = arr[start * chunk_size:end * chunk_size]

        if len(arr) == 0:
            return

        trimmed_sec = len(arr) / 16000
        logger.debug("ASR queued %.1fs audio for transcription (trimmed)", trimmed_sec)

        arr_f32 = arr.astype(np.float32) / 32768.0
        try:
            self._transcription_queue.put_nowait(arr_f32)
        except queue.Full:
            logger.warning("ASR transcription queue full, dropping audio")

    def _transcription_loop(self):
        """
        独立转写线程：从队列取音频，调用 Whisper，触发回调。
        阻塞在 queue.get()，Whisper 运行期间 VAD 正常处理新音频。
        """
        while True:
            arr = self._transcription_queue.get()
            if arr is None:   # 停止信号
                break
            try:
                if self._engine_type == "faster-whisper":
                    segments, _ = self.model.transcribe(
                        arr, language=self.language,
                    )
                    text = "".join(s.text for s in segments).strip()
                else:
                    result = self.model.transcribe(
                        arr, language=self.language, fp16=False,
                    )
                    text = result["text"].strip()

                logger.info("ASR result: %s", text)
                if text and self.on_utterance:
                    self.on_utterance(text, self._seek_generation)
            except Exception as e:
                logger.error("Whisper transcribe error: %s", e)

    def _reset_vad(self):
        self._speaking       = False
        self._audio_buffer   = []
        self._speech_frames  = 0
        self._silence_frames = 0

    def stop(self):
        """关闭转写线程"""
        self._transcription_queue.put(None)
