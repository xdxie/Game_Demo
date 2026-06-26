"""
主动链路端到端测试：用户说话 → ASR 识别 → LLM 回答 → TTS 播报。

用法：
    python tests/test_e2e_active.py

依赖安装：
    pip install miniaudio sounddevice requests -i https://pypi.tuna.tsinghua.edu.cn/simple

GPU 环境变量（ASR 使用 faster-whisper + CUDA 时需要设置）：
    set KMP_DUPLICATE_LIB_OK=TRUE
    set HF_ENDPOINT=https://hf-mirror.com
    set PATH=<conda环境>\\Lib\\site-packages\\nvidia\\cublas\\bin;%PATH%
"""

import sys
import os
import time
import threading
import queue
import json
import base64

import numpy as np
import requests as http_requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from opencc import OpenCC
from backend.config import get_config
from backend.asr.handler import ASRHandler

_t2s = OpenCC("t2s")

# ── LLM 配置 ─────────────────────────────────────────────────────────
LLM_URL = "https://yunwu.ai/v1/chat/completions"
LLM_API_KEY = "sk-rDl2CSNC6PhNFcfnI2jGH7UGnORAhSmgXkgBfAq7cAz2rqKS"
LLM_MODEL = "gemini-3.1-flash-lite:stable"

# ── 火山引擎 TTS 配置 ────────────────────────────────────────────────
VOLC_API_KEY = "d1dca442-e60a-49a6-b296-fa6ae31fd04e"
VOLC_SPEAKER = "zh_female_vv_uranus_bigtts"
VOLC_URL = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"

SYSTEM_PROMPT = (
    "你是一个游戏语音教练，正在实时陪伴玩家观看游戏视频录像。"
    "你能看到当前游戏画面，用口语化的方式回答，1~2句话，不超过40字。"
    "像有经验的老玩家在旁边聊天，简洁有力。"
)

TEST_IMAGE = os.path.join(os.path.dirname(__file__), "..", "assets", "test_game_frame.jpg")


def _load_image_base64() -> str:
    """加载测试图片为 base64"""
    with open(TEST_IMAGE, "rb") as f:
        return base64.b64encode(f.read()).decode()


def call_llm(conversation: list[dict], image_b64: str) -> str:
    """调用 OpenAI 兼容 API（带图片），返回回答文本"""
    headers = {
        "Authorization": f"Bearer {LLM_API_KEY}",
        "Content-Type": "application/json",
    }

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for msg in conversation:
        if msg["role"] == "user":
            messages.append({
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
                    },
                    {"type": "text", "text": msg["content"]},
                ],
            })
        else:
            messages.append(msg)

    payload = {
        "model": LLM_MODEL,
        "messages": messages,
        "max_tokens": 120,
        "temperature": 0.7,
    }
    resp = http_requests.post(LLM_URL, headers=headers, json=payload, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"].strip()


def play_tts(text: str) -> tuple[float, float]:
    """
    火山引擎 TTS 流式播放，返回 (首包时延ms, 播放总时长ms)。
    """
    import miniaudio
    import sounddevice as sd

    mp3_buf = bytearray()
    pcm_queue = queue.Queue()
    synthesis_done = threading.Event()
    first_chunk_time = [None]
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
            "X-Api-Key": VOLC_API_KEY,
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
                "speaker": VOLC_SPEAKER,
                "additions": additions,
                "audio_params": {
                    "format": "mp3",
                    "sample_rate": 24000,
                    "speed_ratio": 1.5,
                },
            }
        }
        session = http_requests.Session()
        try:
            resp = session.post(VOLC_URL, headers=headers, json=payload, stream=True, timeout=(5, 30))
            if resp.status_code != 200:
                print(f"  TTS HTTP {resp.status_code}: {resp.text[:200]}")
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
                    print(f"  TTS API 错误: code={code} msg={data.get('message', '')}")
                    break
            resp.close()
        except Exception as e:
            print(f"  TTS 请求失败: {e}")
        finally:
            session.close()
        synthesis_done.set()

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
        return (0, 0)

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
    return (first_ms, total_ms)


def record_and_recognize(handler: ASRHandler, sample_rate: int = 16000) -> tuple[str, float]:
    """
    录音 → ASR 识别，返回 (识别文本, 时延ms)。
    """
    import sounddevice as sd

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
    print("  录音中... 按回车停止")
    input()
    stop_flag.set()
    stream.stop()
    stream.close()

    if not recording:
        return ("", 0)

    audio_data = np.concatenate(recording, axis=0).flatten()
    audio_bytes = audio_data.astype(np.int16).tobytes()
    duration_sec = len(audio_data) / sample_rate
    amplitude = float(np.abs(audio_data).mean())

    print(f"  录音 {duration_sec:.1f}s  振幅 {amplitude:.0f}")

    handler.SILENCE_THRESHOLD = max(50, int(amplitude * 0.4))
    handler._reset_vad()
    handler._muted = False

    result_text = []
    done = threading.Event()
    handler.on_utterance = lambda text: (result_text.append(text), done.set())

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

    handler.on_utterance = None

    if result_text:
        return (_t2s.convert(result_text[0]), latency_ms)
    return ("", latency_ms)


def main():
    try:
        import miniaudio  # noqa: F401
    except ImportError:
        print("需要安装 miniaudio：pip install miniaudio -i https://pypi.tuna.tsinghua.edu.cn/simple")
        return
    try:
        import sounddevice as sd  # noqa: F401
    except ImportError:
        print("需要安装 sounddevice：pip install sounddevice -i https://pypi.tuna.tsinghua.edu.cn/simple")
        return

    cfg = get_config()
    print(f"[端到端测试] ASR: {cfg.asr_engine}/{cfg.whisper_model}  TTS: 火山引擎  LLM: {LLM_MODEL}")
    print("加载 Whisper 模型...")

    handler = ASRHandler(
        model_size=cfg.whisper_model,
        language=cfg.whisper_language,
        engine=cfg.asr_engine,
        device=cfg.asr_device,
    )
    print(f"模型就绪 ({handler._engine_type})")

    print("加载测试图片...")
    image_b64 = _load_image_base64()
    print(f"图片就绪：{os.path.basename(TEST_IMAGE)}")

    conversation: list[dict] = []

    print("\n按回车开始说话，再按回车停止，LLM 回答后 TTS 播报。输入 q 退出。")
    print("支持多轮对话，上下文自动保留。\n")

    while True:
        cmd = input("按回车开始说话 (q 退出): ").strip()
        if cmd.lower() == "q":
            break

        # ── 1. ASR ────────────────────────────────────────────────────
        print("[ASR]")
        user_text, asr_ms = record_and_recognize(handler)
        if not user_text:
            print("  未识别到语音\n")
            continue
        print(f"  识别：{user_text}（{asr_ms:.0f}ms）")

        # ── 2. LLM ────────────────────────────────────────────────────
        print("[LLM]")
        conversation.append({"role": "user", "content": user_text})
        t_llm = time.perf_counter()
        try:
            reply = call_llm(conversation, image_b64)
        except Exception as e:
            print(f"  调用失败: {e}\n")
            conversation.pop()
            continue
        llm_ms = (time.perf_counter() - t_llm) * 1000
        conversation.append({"role": "assistant", "content": reply})
        print(f"  回答：{reply}（{llm_ms:.0f}ms）")

        # ── 3. TTS ────────────────────────────────────────────────────
        print("[TTS]")
        tts_first_ms, tts_total_ms = play_tts(reply)
        print(f"  首包：{tts_first_ms:.0f}ms  播放：{tts_total_ms:.0f}ms")

        # ── 汇总 ──────────────────────────────────────────────────────
        total_ms = asr_ms + llm_ms + tts_total_ms
        print(f"\n  ═══ ASR {asr_ms:.0f}ms + LLM {llm_ms:.0f}ms + TTS {tts_total_ms:.0f}ms = 总计 {total_ms:.0f}ms ═══\n")

    handler.stop()
    print("测试结束")


if __name__ == "__main__":
    main()
