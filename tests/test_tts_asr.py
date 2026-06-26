"""
TTS / ASR 交互式测试（可选，需本机麦克风与网络）。

用法：
    python tests/test_tts_asr.py tts         # TTS 测试（火山引擎，需 VOLC_API_KEY）
    python tests/test_tts_asr.py tts-edge    # TTS 测试（edge-tts）
    python tests/test_tts_asr.py asr         # ASR 语音识别测试（录音 → 识别）

依赖安装：
    pip install miniaudio sounddevice
"""

from __future__ import annotations

import os
import sys
import time
import threading
import queue

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.config import get_config
from backend.tts.engine import TTSEngine


def run_tts_edge():
    """edge-tts 测试：合成完整 MP3 后播放"""
    try:
        import miniaudio
        import sounddevice as sd
    except ImportError:
        print("需要安装：pip install miniaudio sounddevice")
        return

    cfg = get_config()
    engine = TTSEngine(engine="edge-tts", voice=cfg.tts_voice, rate=cfg.tts_rate)

    print(f"[TTS] edge-tts  音色: {cfg.tts_voice}  语速: {cfg.tts_rate}")
    print("输入文本后回车，输入 q 退出\n")

    while True:
        text = input("请输入文本: ").strip()
        if not text or text.lower() == "q":
            break

        chunks: list[bytes] = []

        def on_audio(data: bytes):
            chunks.append(data)

        engine.on_audio_data = on_audio
        done = threading.Event()

        def worker():
            engine.speak_async(text, on_dispatched=lambda _: done.set())
            done.wait(timeout=30)

        print("合成中...")
        t0 = time.perf_counter()
        worker()
        mp3 = b"".join(chunks)
        if not mp3:
            print("合成失败\n")
            continue

        decoded = miniaudio.decode(mp3, nchannels=1)
        pcm = decoded.samples.tobytes()
        out = sd.RawOutputStream(
            samplerate=decoded.sample_rate, channels=1, dtype="int16",
        )
        out.start()
        out.write(pcm)
        out.stop()
        out.close()
        print(f"播放完成，耗时 {(time.perf_counter() - t0) * 1000:.0f}ms\n")


def run_tts():
    """火山引擎 TTS 测试"""
    cfg = get_config()
    if not cfg.volc_api_key:
        print("请在 .env 中设置 VOLC_API_KEY")
        return
    try:
        import miniaudio
        import sounddevice as sd
    except ImportError:
        print("需要安装：pip install miniaudio sounddevice")
        return

    engine = TTSEngine(
        engine="volcengine",
        volc_api_key=cfg.volc_api_key,
        volc_speaker=cfg.volc_speaker,
        volc_speed_ratio=cfg.volc_speed_ratio,
    )

    print(f"[TTS] volcengine  音色: {cfg.volc_speaker}")
    print("输入文本后回车，输入 q 退出\n")

    while True:
        text = input("请输入文本: ").strip()
        if not text or text.lower() == "q":
            break

        chunks: list[bytes] = []
        engine.on_audio_data = chunks.append
        done = threading.Event()
        engine.speak_async(text, on_dispatched=lambda _: done.set())
        done.wait(timeout=30)

        mp3 = b"".join(chunks)
        if not mp3:
            print("合成失败\n")
            continue

        decoded = miniaudio.decode(mp3, nchannels=1)
        pcm = decoded.samples.tobytes()
        out = sd.RawOutputStream(
            samplerate=decoded.sample_rate, channels=1, dtype="int16",
        )
        out.start()
        out.write(pcm)
        out.stop()
        out.close()
        print("播放完成\n")


def run_asr():
    """ASR 交互测试"""
    try:
        import sounddevice as sd
    except ImportError:
        print("需要安装：pip install sounddevice")
        return

    import numpy as np
    from backend.asr.handler import ASRHandler

    cfg = get_config()
    print(
        f"[ASR] 引擎: {cfg.asr_engine}  模型: {cfg.whisper_model}  设备: {cfg.asr_device}"
    )
    print("加载模型中...")

    handler = ASRHandler(
        model_size=cfg.whisper_model,
        language=cfg.whisper_language,
        engine=cfg.asr_engine,
        device=cfg.asr_device,
    )
    print(f"模型就绪 ({handler._engine_type})")
    print("按回车开始录音，再按回车停止，输入 q 退出\n")

    sample_rate = 16000

    while True:
        cmd = input("按回车开始录音 (q 退出): ").strip()
        if cmd.lower() == "q":
            break

        recording = []
        stop_flag = threading.Event()

        def audio_callback(indata, frames, time_info, status):
            if not stop_flag.is_set():
                recording.append(indata.copy())

        stream = sd.InputStream(
            samplerate=sample_rate, channels=1, dtype="int16",
            blocksize=1600, callback=audio_callback,
        )
        stream.start()
        print("录音中... 按回车停止")
        input()
        stop_flag.set()
        stream.stop()
        stream.close()

        if not recording:
            print("未录到音频\n")
            continue

        audio_data = np.concatenate(recording, axis=0).flatten()
        audio_bytes = audio_data.astype(np.int16).tobytes()

        handler._reset_vad()
        handler._muted = False

        result_text = []
        done = threading.Event()
        handler.on_utterance = lambda text, gen=0: (result_text.append(text), done.set())

        t0 = time.perf_counter()
        chunk_size = 2 * 1600
        for i in range(0, len(audio_bytes), chunk_size):
            chunk = audio_bytes[i:i + chunk_size]
            if len(chunk) == chunk_size:
                handler.process_audio_chunk(chunk, sample_rate=sample_rate)

        silence = np.zeros(1600, dtype=np.int16).tobytes()
        for _ in range(30):
            handler.process_audio_chunk(silence, sample_rate=sample_rate)

        done.wait(timeout=15.0)
        latency_ms = (time.perf_counter() - t0) * 1000

        if result_text:
            print(f"识别结果：{result_text[0]}")
        else:
            print("识别结果：(无)")
        print(f"时延：{latency_ms:.0f}ms\n")

    handler.stop()


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ("tts", "tts-edge", "asr"):
        print("用法：")
        print("  python tests/test_tts_asr.py tts")
        print("  python tests/test_tts_asr.py tts-edge")
        print("  python tests/test_tts_asr.py asr")
        sys.exit(1)

    if sys.argv[1] == "tts":
        run_tts()
    elif sys.argv[1] == "tts-edge":
        run_tts_edge()
    else:
        run_asr()
