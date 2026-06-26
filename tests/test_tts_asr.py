"""
TTS / ASR 交互式测试。

用法：
    python tests/test_tts_asr.py tts         # TTS 测试（火山引擎，默认）
    python tests/test_tts_asr.py tts-edge    # TTS 测试（edge-tts，对比用）
    python tests/test_tts_asr.py asr         # ASR 语音识别测试（录音 → 识别）

依赖安装：
    pip install miniaudio sounddevice -i https://pypi.tuna.tsinghua.edu.cn/simple

GPU 环境变量（ASR 使用 faster-whisper + CUDA 时需要设置）：
    set KMP_DUPLICATE_LIB_OK=TRUE
    set HF_ENDPOINT=https://hf-mirror.com
    set PATH=<conda环境>\\Lib\\site-packages\\nvidia\\cublas\\bin;%PATH%

没有 GPU 的同学：在 backend/config.py 中设置 asr_device="cpu"，无需上述配置。
"""

import sys
import os
import time
import threading
import queue
import json
import base64

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from opencc import OpenCC
from backend.config import get_config
from backend.tts.engine import TTSEngine

_t2s = OpenCC("t2s")


def _normalize(text: str) -> str:
    return _t2s.convert(text)


def run_tts_edge():
    """edge-tts 测试（对比用）：流式合成，边收边播"""
    import asyncio

    try:
        import miniaudio
    except ImportError:
        print("需要安装 miniaudio：pip install miniaudio -i https://pypi.tuna.tsinghua.edu.cn/simple")
        return
    try:
        import sounddevice as sd
    except ImportError:
        print("需要安装 sounddevice：pip install sounddevice -i https://pypi.tuna.tsinghua.edu.cn/simple")
        return

    cfg = get_config()
    engine = TTSEngine(voice=cfg.tts_voice, rate=cfg.tts_rate)

    print(f"[TTS 测试] 引擎: edge-tts（边收边播）  音色: {cfg.tts_voice}  语速: {cfg.tts_rate}")
    print("输入文本后回车，收到音频立即播放。输入 q 退出\n")

    while True:
        text = input("请输入文本: ").strip()
        if not text or text.lower() == "q":
            break

        mp3_buf = bytearray()
        pcm_queue = queue.Queue()
        synthesis_done = threading.Event()
        first_chunk_time = [None]
        first_play_time = [None]
        decoded_pcm_len = [0]

        def on_chunk(data):
            if first_chunk_time[0] is None:
                first_chunk_time[0] = time.perf_counter()
            mp3_buf.extend(data)
            try:
                decoded = miniaudio.decode(
                    bytes(mp3_buf), nchannels=1,
                    output_format=miniaudio.SampleFormat.SIGNED16,
                )
                raw = decoded.samples.tobytes()
                new_pcm = raw[decoded_pcm_len[0]:]
                if new_pcm:
                    pcm_queue.put((new_pcm, decoded.sample_rate))
                    decoded_pcm_len[0] = len(raw)
            except Exception:
                pass

        def do_synthesis():
            loop = asyncio.new_event_loop()
            engine.on_audio_data = on_chunk
            loop.run_until_complete(engine._synthesize_streaming(text))
            synthesis_done.set()

        print("合成中...")
        t0 = time.perf_counter()
        threading.Thread(target=do_synthesis, daemon=True).start()

        first_pcm = None
        sample_rate = 24000
        while first_pcm is None:
            try:
                first_pcm, sample_rate = pcm_queue.get(timeout=30)
            except queue.Empty:
                break

        if first_pcm is None:
            print("合成失败\n")
            continue

        first_play_time[0] = time.perf_counter()
        out = sd.RawOutputStream(samplerate=sample_rate, channels=1, dtype="int16")
        out.start()
        out.write(first_pcm)

        while True:
            try:
                pcm, _ = pcm_queue.get(timeout=0.5)
                out.write(pcm)
            except queue.Empty:
                if synthesis_done.is_set() and pcm_queue.empty():
                    break

        out.stop()
        out.close()

        total_ms = (time.perf_counter() - t0) * 1000
        first_ms = (first_chunk_time[0] - t0) * 1000 if first_chunk_time[0] else total_ms
        play_ms = (first_play_time[0] - t0) * 1000 if first_play_time[0] else total_ms

        print(f"文本：{text}")
        print(f"首包到达：{first_ms:.0f}ms  开始播放：{play_ms:.0f}ms  播放结束：{total_ms:.0f}ms\n")


def run_tts():
    """火山引擎 TTS 测试（默认）：V3 流式合成，边收边播"""
    try:
        import miniaudio
    except ImportError:
        print("需要安装 miniaudio：pip install miniaudio -i https://pypi.tuna.tsinghua.edu.cn/simple")
        return
    try:
        import sounddevice as sd
    except ImportError:
        print("需要安装 sounddevice：pip install sounddevice -i https://pypi.tuna.tsinghua.edu.cn/simple")
        return
    try:
        import requests
    except ImportError:
        print("需要安装 requests：pip install requests -i https://pypi.tuna.tsinghua.edu.cn/simple")
        return
    import numpy as np

    api_key = "d1dca442-e60a-49a6-b296-fa6ae31fd04e"
    speaker = "zh_female_vv_uranus_bigtts"
    url = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"

    print(f"[TTS 测试] 引擎: 火山引擎 seed-tts-2.0（边收边播）  音色: {speaker}")
    print("输入文本后回车，收到音频立即播放。输入 q 退出\n")

    while True:
        text = input("请输入文本: ").strip()
        if not text or text.lower() == "q":
            break

        mp3_buf = bytearray()
        pcm_queue = queue.Queue()
        synthesis_done = threading.Event()
        first_chunk_time = [None]
        first_play_time = [None]
        decoded_pcm_len = [0]
        volume_gain = 3.0

        def process_mp3_chunk(mp3_data):
            if first_chunk_time[0] is None:
                first_chunk_time[0] = time.perf_counter()
            mp3_buf.extend(mp3_data)
            try:
                decoded = miniaudio.decode(
                    bytes(mp3_buf), nchannels=1,
                    output_format=miniaudio.SampleFormat.SIGNED16,
                )
                raw = decoded.samples.tobytes()
                new_pcm = raw[decoded_pcm_len[0]:]
                if new_pcm:
                    samples = np.frombuffer(new_pcm, dtype=np.int16)
                    amplified = np.clip(samples * volume_gain, -32768, 32767).astype(np.int16)
                    pcm_queue.put((amplified.tobytes(), decoded.sample_rate))
                    decoded_pcm_len[0] = len(raw)
            except Exception:
                pass

        def do_synthesis():
            headers = {
                "X-Api-Key": api_key,
                "X-Api-Resource-Id": "seed-tts-2.0",
                "Content-Type": "application/json",
                "Connection": "keep-alive",
                "X-Control-Require-Usage-Tokens-Return": "*",
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
                    "speaker": speaker,
                    "additions": additions,
                    "audio_params": {
                        "format": "mp3",
                        "sample_rate": 24000,
                        "speed_ratio": 1.5,
                    },
                }
            }
            session = requests.Session()
            try:
                resp = session.post(url, headers=headers, json=payload, stream=True, timeout=(5, 30))
                if resp.status_code != 200:
                    print(f"  → HTTP {resp.status_code}: {resp.text[:300]}")
                    synthesis_done.set()
                    return
                for line in resp.iter_lines(decode_unicode=True):
                    if not line:
                        continue
                    data = json.loads(line)
                    code = data.get("code", 0)
                    if code == 0 and data.get("data"):
                        chunk_audio = base64.b64decode(data["data"])
                        process_mp3_chunk(chunk_audio)
                    elif code == 20000000:
                        break
                    elif code > 0:
                        print(f"  → API 错误: code={code} msg={data.get('message', '')}")
                        break
                resp.close()
            except requests.exceptions.ConnectTimeout:
                print("  → 连接超时（5s）")
            except requests.exceptions.ReadTimeout:
                print("  → 读取超时（30s）")
            except Exception as e:
                print(f"  → 请求失败: {e}")
            finally:
                session.close()
            synthesis_done.set()

        print("合成中...")
        t0 = time.perf_counter()
        threading.Thread(target=do_synthesis, daemon=True).start()

        first_pcm = None
        sample_rate = 24000
        while first_pcm is None:
            try:
                first_pcm, sample_rate = pcm_queue.get(timeout=30)
            except queue.Empty:
                if synthesis_done.is_set():
                    break

        if first_pcm is None:
            print("合成失败\n")
            continue

        first_play_time[0] = time.perf_counter()
        out = sd.RawOutputStream(samplerate=sample_rate, channels=1, dtype="int16")
        out.start()
        out.write(first_pcm)

        while True:
            try:
                pcm, _ = pcm_queue.get(timeout=0.5)
                out.write(pcm)
            except queue.Empty:
                if synthesis_done.is_set() and pcm_queue.empty():
                    break

        out.stop()
        out.close()

        total_ms = (time.perf_counter() - t0) * 1000
        first_ms = (first_chunk_time[0] - t0) * 1000 if first_chunk_time[0] else total_ms
        play_ms = (first_play_time[0] - t0) * 1000 if first_play_time[0] else total_ms

        print(f"文本：{text}")
        print(f"首包到达：{first_ms:.0f}ms  开始播放：{play_ms:.0f}ms  播放结束：{total_ms:.0f}ms\n")


def run_asr():
    """ASR 交互测试：用户说话，识别后打印文本和时延"""
    try:
        import sounddevice as sd
    except ImportError:
        print("需要安装 sounddevice：pip install sounddevice -i https://pypi.tuna.tsinghua.edu.cn/simple")
        return

    import numpy as np
    from backend.asr.handler import ASRHandler

    cfg = get_config()
    print(f"[ASR 测试] 引擎: {cfg.asr_engine}  模型: {cfg.whisper_model}  设备: {cfg.asr_device}")
    print("加载模型中...")

    handler = ASRHandler(
        model_size=cfg.whisper_model,
        language=cfg.whisper_language,
        engine=cfg.asr_engine,
        device=cfg.asr_device,
    )
    print(f"模型就绪 ({handler._engine_type})")
    print("按回车开始录音，再按回车停止录音，输入 q 退出\n")

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
            blocksize=1600, callback=audio_callback
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
        duration_sec = len(audio_data) / sample_rate
        amplitude = float(np.abs(audio_data).mean())

        print(f"录音时长：{duration_sec:.1f}s  平均振幅：{amplitude:.0f}（阈值：{handler.SILENCE_THRESHOLD}）")

        handler.SILENCE_THRESHOLD = max(50, int(amplitude * 0.4))
        print(f"自动调整阈值 → {handler.SILENCE_THRESHOLD}")

        handler._reset_vad()
        handler._muted = False

        result_text = []
        done = threading.Event()
        handler.on_utterance = lambda text: (result_text.append(text), done.set())

        print("开始识别...")
        t0 = time.perf_counter()

        chunk_size = 2 * 1600
        fed = 0
        for i in range(0, len(audio_bytes), chunk_size):
            chunk = audio_bytes[i:i + chunk_size]
            if len(chunk) == chunk_size:
                handler.process_audio_chunk(chunk, sample_rate=sample_rate)
                fed += 1

        silence = np.zeros(1600, dtype=np.int16).tobytes()
        for _ in range(30):
            handler.process_audio_chunk(silence, sample_rate=sample_rate)

        print(f"已送入 {fed} 帧 + 30 帧静音，等待识别结果...")

        done.wait(timeout=15.0)
        latency_ms = (time.perf_counter() - t0) * 1000

        handler.on_utterance = None

        if result_text:
            raw = result_text[0]
            normalized = _normalize(raw)
            print(f"转换文本：{normalized}")
        else:
            print("转换文本：(无识别结果)")

        print(f"结束，时延：{latency_ms:.0f}ms，音频时长：{duration_sec:.1f}s\n")

    handler.stop()


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ("tts", "tts-edge", "asr"):
        print("用法：")
        print("  python tests/test_tts_asr.py tts         # TTS 测试（火山引擎，默认）")
        print("  python tests/test_tts_asr.py tts-edge    # TTS 测试（edge-tts，对比用）")
        print("  python tests/test_tts_asr.py asr         # ASR 测试")
        sys.exit(1)

    if sys.argv[1] == "tts":
        run_tts()
    elif sys.argv[1] == "tts-edge":
        run_tts_edge()
    else:
        run_asr()
