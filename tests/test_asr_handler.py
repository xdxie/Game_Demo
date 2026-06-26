"""
测试 backend/asr/handler.py
Whisper 模型通过 patch 替换为 mock，测试 VAD 逻辑和非阻塞转写架构。
"""

import time
import threading
import numpy as np
import pytest
from unittest.mock import MagicMock, patch


# ── Fixture ───────────────────────────────────────────────────────────

@pytest.fixture
def asr_handler():
    """
    创建 ASRHandler 并 mock 掉 whisper.load_model。
    mock model 的 transcribe() 返回固定文本 "测试语音"。
    """
    with patch("whisper.load_model") as mock_load:
        mock_model = MagicMock()
        mock_model.transcribe.return_value = {"text": "测试语音"}
        mock_load.return_value = mock_model

        from backend.asr.handler import ASRHandler
        handler = ASRHandler(model_size="base", language="zh")
        yield handler
        handler.stop()   # 停止转写线程


def make_pcm(amplitude: int, n_samples: int = 1600) -> bytes:
    """生成振幅为 amplitude 的 PCM bytes（int16 little-endian）"""
    arr = np.full(n_samples, amplitude, dtype=np.int16)
    return arr.tobytes()


SILENT_CHUNK = make_pcm(0)       # 无声音
LOUD_CHUNK   = make_pcm(1000)    # 高于默认阈值 300


# ═══════════════════════════════════════════════════════════════════════
# VAD 状态机
# ═══════════════════════════════════════════════════════════════════════

class TestVAD:
    def test_silence_does_not_start_speech(self, asr_handler):
        asr_handler.process_audio_chunk(SILENT_CHUNK)
        assert asr_handler._speaking is False
        assert len(asr_handler._audio_buffer) == 0

    def test_loud_chunk_starts_speech(self, asr_handler):
        asr_handler.process_audio_chunk(LOUD_CHUNK)
        assert asr_handler._speaking is True
        assert asr_handler._speech_frames == 1

    def test_speech_then_silence_flushes(self, asr_handler):
        """
        发送足够多的静音块（> SILENCE_END_SEC）触发 _flush()。
        SILENCE_END_SEC=1.2，每块 100ms → 需要 ≥ 12 个静音块。
        """
        done = threading.Event()
        asr_handler.on_utterance = lambda text, gen=0: done.set()

        # 语音段（超过 SPEECH_MIN_SEC=0.5 → ≥ 5 块）
        for _ in range(8):
            asr_handler.process_audio_chunk(LOUD_CHUNK)

        # 静音段（超过 SILENCE_END_SEC=1.2 → ≥ 13 块）
        for _ in range(15):
            asr_handler.process_audio_chunk(SILENT_CHUNK)

        # 等转写线程完成（最多 3 秒）
        done.wait(timeout=3.0)
        assert done.is_set(), "on_utterance 未在超时内被调用"

    def test_short_speech_not_flushed(self, asr_handler):
        """语音时长 < SPEECH_MIN_SEC → 不触发识别"""
        called = threading.Event()
        asr_handler.on_utterance = lambda text, gen=0: called.set()

        # 只发 1 个语音块（100ms << SPEECH_MIN_SEC=0.5s）
        asr_handler.process_audio_chunk(LOUD_CHUNK)

        # 发足够多静音块触发 silence timeout
        for _ in range(15):
            asr_handler.process_audio_chunk(SILENT_CHUNK)

        # 等待 0.5s，不应有回调
        called.wait(timeout=0.5)
        assert not called.is_set()

    def test_reset_vad_clears_state(self, asr_handler):
        asr_handler.process_audio_chunk(LOUD_CHUNK)
        asr_handler._reset_vad()
        assert asr_handler._speaking is False
        assert asr_handler._speech_frames == 0
        assert asr_handler._silence_frames == 0
        assert len(asr_handler._audio_buffer) == 0


# ═══════════════════════════════════════════════════════════════════════
# TTS mute/unmute 联动
# ═══════════════════════════════════════════════════════════════════════

class TestMuteUnmute:
    def test_mute_blocks_processing(self, asr_handler):
        """mute 后的音频块不更新 VAD 状态"""
        asr_handler.mute()
        asr_handler.process_audio_chunk(LOUD_CHUNK)
        assert asr_handler._speaking is False

    def test_mute_resets_vad(self, asr_handler):
        """mute 时清空已有的 VAD 状态"""
        asr_handler.process_audio_chunk(LOUD_CHUNK)
        assert asr_handler._speaking is True
        asr_handler.mute()
        assert asr_handler._speaking is False
        assert asr_handler._audio_buffer == []

    def test_unmute_delayed(self, asr_handler):
        """unmute 有 TTS_MUTE_TAIL_SEC 延迟"""
        asr_handler.mute()
        assert asr_handler._muted is True
        asr_handler.unmute()
        assert asr_handler._muted is True
        time.sleep(0.5)
        assert asr_handler._muted is False

    def test_force_unmute_immediate(self, asr_handler):
        """force_unmute 跳过延迟，立即恢复"""
        asr_handler.mute()
        assert asr_handler._muted is True
        asr_handler.force_unmute()
        assert asr_handler._muted is False

    def test_repeated_unmute_single_timer(self, asr_handler):
        """多次 unmute 不叠加多个有效 timer"""
        asr_handler.mute()
        asr_handler.unmute()
        asr_handler.unmute()
        assert asr_handler._unmute_timer is not None
        time.sleep(0.5)
        assert asr_handler._muted is False

    def test_force_unmute_cancels_pending_timer(self, asr_handler):
        asr_handler.mute()
        asr_handler.unmute()
        assert asr_handler._unmute_timer is not None
        asr_handler.force_unmute()
        assert asr_handler._unmute_timer is None
        assert asr_handler._muted is False


class TestASRStateCallback:
    def test_initial_state_listening(self, asr_handler):
        assert asr_handler._last_emitted_state == "listening"

    def test_mute_emits_muted(self, asr_handler):
        states = []
        asr_handler.on_state_change = states.append
        asr_handler.mute()
        assert "muted" in states

    def test_recording_state_on_loud_chunk(self, asr_handler):
        states = []
        asr_handler.on_state_change = states.append
        asr_handler.process_audio_chunk(LOUD_CHUNK)
        assert "recording" in states

    def test_flush_keeps_processing_until_transcription_done(self, asr_handler):
        """语音结束后应保持 processing，不应立即变 listening"""
        for _ in range(8):
            asr_handler.process_audio_chunk(LOUD_CHUNK)
        for _ in range(15):
            asr_handler.process_audio_chunk(SILENT_CHUNK)
        assert asr_handler._activity_state == "processing"
        assert asr_handler._transcription_inflight == 1

    def test_unmute_restores_listening_after_muted_transcription(self, asr_handler):
        """TTS 期间完成转写，unmute 后应恢复 listening 而非卡在 processing"""
        asr_handler._gen_inflight = 1
        asr_handler._set_activity("processing")
        asr_handler.mute()
        asr_handler._gen_inflight = 0
        asr_handler.unmute()
        time.sleep(0.5)
        assert asr_handler._last_emitted_state == "listening"

    def test_mute_clears_recording_state(self, asr_handler):
        """TTS mute 时应清除 recording 状态，避免 unmute 后 UI 卡在正在说话"""
        asr_handler.process_audio_chunk(LOUD_CHUNK)
        assert asr_handler._activity_state == "recording"
        asr_handler.mute()
        assert asr_handler._activity_state == "listening"
        assert asr_handler._last_emitted_state == "muted"


# ═══════════════════════════════════════════════════════════════════════
# 非阻塞转写架构（Fix 13）
# ═══════════════════════════════════════════════════════════════════════

class TestNonBlockingTranscription:
    def test_flush_is_non_blocking(self, asr_handler):
        """_flush() 应立即返回（不阻塞等 Whisper）"""
        # 制造足量语音 buffer
        for _ in range(8):
            asr_handler._audio_buffer.append(LOUD_CHUNK)
        asr_handler._speech_frames = 8

        start = time.time()
        asr_handler._flush()
        elapsed = time.time() - start
        # 应该立即返回（< 100ms），不是在 Whisper 内阻塞
        assert elapsed < 0.1, f"_flush() 阻塞了 {elapsed:.3f}s"

    def test_transcription_thread_calls_on_utterance(self, asr_handler):
        """转写线程应在完成后调用 on_utterance"""
        results = []
        asr_handler.on_utterance = lambda text, gen=0: results.append(text)

        # 直接向转写队列推任务
        arr = np.zeros(16000, dtype=np.float32)
        asr_handler._transcription_queue.put((arr, asr_handler._seek_generation))

        # 等转写线程处理（最多 3s）
        deadline = time.time() + 3.0
        while not results and time.time() < deadline:
            time.sleep(0.05)

        assert len(results) == 1
        assert results[0] == "测试语音"   # mock model 的返回值

    def test_vad_continues_during_transcription(self, asr_handler):
        """
        转写线程阻塞期间，VAD 应能继续接收新音频。
        验证方式：在转写线程忙时推入新语音，_speaking 状态应更新。
        """
        # 让 transcribe 阻塞 0.5s
        block_event = threading.Event()
        original = asr_handler.model.transcribe

        def slow_transcribe(*args, **kwargs):
            block_event.wait(timeout=1.0)
            return {"text": ""}

        asr_handler.model.transcribe.side_effect = slow_transcribe

        # 推入一段语音触发转写
        for _ in range(8):
            asr_handler._audio_buffer.append(LOUD_CHUNK)
        asr_handler._speech_frames = 8
        asr_handler._flush()
        asr_handler._reset_vad()   # 模拟 VAD 已重置

        # 转写进行中，新语音仍可被接收
        asr_handler.process_audio_chunk(LOUD_CHUNK)
        assert asr_handler._speaking is True   # 新语音被正常检测

        block_event.set()  # 解除阻塞

    def test_queue_full_drops_audio(self, asr_handler):
        """转写队列满时，_flush() 应丢弃而不是阻塞（maxsize=4）"""
        # 填满队列
        import numpy as np
        for _ in range(5):
            asr_handler._transcription_queue.put(
                (np.zeros(100), asr_handler._seek_generation)
            )

        # 准备缓冲区
        for _ in range(8):
            asr_handler._audio_buffer.append(LOUD_CHUNK)
        asr_handler._speech_frames = 8

        # 不应阻塞
        start = time.time()
        asr_handler._flush()
        elapsed = time.time() - start
        assert elapsed < 0.1


class TestSeekReset:
    def test_reset_for_seek_discards_stale_transcription(self, asr_handler):
        """Seek 后旧 generation 的转写结果应被丢弃，不触发 on_utterance。"""
        results = []
        asr_handler.on_utterance = lambda text, gen=0: results.append(text)
        stale_gen = asr_handler._seek_generation

        asr_handler.reset_for_seek()

        assert asr_handler._seek_generation == stale_gen + 1
        assert asr_handler._transcription_inflight == 0
        assert asr_handler._gen_inflight == 0
        assert asr_handler._activity_state == "listening"

        asr_handler._transcription_queue.put((np.zeros(100), stale_gen))
        time.sleep(0.3)

        assert results == []

    def test_seek_generation_property(self, asr_handler):
        assert asr_handler.seek_generation == 0
        asr_handler.reset_for_seek()
        assert asr_handler.seek_generation == 1

    def test_seek_during_transcribe_discards_callback(self, asr_handler):
        """seek 发生在 transcribe 完成与回调之间时，不应触发 on_utterance"""
        results = []
        asr_handler.on_utterance = lambda text, gen: results.append((text, gen))

        def transcribe_then_seek(*args, **kwargs):
            asr_handler.reset_for_seek()
            return {"text": "幽灵问题"}

        asr_handler.model.transcribe.side_effect = transcribe_then_seek
        stale_gen = asr_handler.seek_generation
        asr_handler._transcription_queue.put((np.zeros(100), stale_gen))
        time.sleep(0.3)

        assert results == []
