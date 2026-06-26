"""
全局配置。所有可调参数集中于此，每个参数都标注了负责调优的角色。
"""

from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class Config:
    # ── NitroGen 连接 ─────────────────────────────────────────────────
    nitrogen_server: str = "tcp://localhost:5555"
    nitrogen_target_fps: float = 10.0    # 向 NitroGen 发送的帧率（推理约200ms/chunk）

    # ── 快系统：动作过滤阈值（2号负责调优）────────────────────────────
    fast_trigger_confidence: float = 0.75   # 快通道触发置信度下限
    sustained_danger_sec: float = 3.0       # DODGE 持续多久触发 SUSTAINED_DANGER

    # 各事件类型冷却时间（秒）
    cooldowns: dict = field(default_factory=lambda: {
        "sudden_dodge":      3.0,
        "attack_window":     4.0,
        "sustained_danger":  8.0,
        "movement_shift":   10.0,
        "pattern_completed": 5.0,
    })

    # ── 慢系统：VLM（4号负责调优）────────────────────────────────────
    vlm_model: str = "claude-sonnet-4-6"
    vlm_max_tokens: int = 120           # 限制回答字数（约 40 字 + buffer）
    context_window_sec: float = 15.0    # 上下文缓冲区时间窗口
    slow_max_queue_age: float = 8.0     # 慢系统 TTS 结果有效期（秒）
    vlm_dedup_sec: float = 5.0          # 同类事件 VLM 去重窗口

    # ── TTS（3号负责调优）─────────────────────────────────────────────
    # TTS 引擎选择：
    #   "volcengine" — 火山引擎 seed-tts-2.0，国内服务器，首包 ~600ms
    #   "edge-tts"   — 微软 Azure，免费无需 key，首包 ~1s
    tts_engine: str = "volcengine"

    # 火山引擎配置（tts_engine="volcengine" 时生效）
    volc_api_key: str = "d1dca442-e60a-49a6-b296-fa6ae31fd04e"
    volc_speaker: str = "zh_female_vv_uranus_bigtts"
    volc_speed_ratio: float = 1.5

    # edge-tts 配置（tts_engine="edge-tts" 时生效）
    tts_voice: str = "zh-CN-XiaoxiaoNeural"  # edge-tts 声音（3号选音色）
    tts_rate: str = "+50%"                  # 语速（+50% 快节奏，游戏场景）

    tts_inter_utterance_gap: float = 0.8    # 两条语音之间的间隔（秒）
    fast_hint_expire_sec: float = 2.0       # 快提示超时丢弃（秒）

    # ── ASR（5号负责调优）─────────────────────────────────────────────
    whisper_model: str = "base"
    whisper_language: str = "zh"
    # ASR 引擎选择：
    #   "faster-whisper" — 推荐，CTranslate2 加速，比 openai-whisper 快 4-6 倍
    #   "openai-whisper" — 原版，无 GPU 依赖问题，兼容性好
    asr_engine: str = "faster-whisper"
    # ASR 推理设备（仅 faster-whisper 生效）：
    #   "auto" — 有 CUDA 用 GPU，否则 CPU
    #   "cuda" — 强制 GPU（需要 nvidia-cublas-cu12）
    #   "cpu"  — 强制 CPU（int8 量化，仍比 openai-whisper 快）
    asr_device: str = "cuda"
    vad_silence_threshold: int = 300        # 振幅静音阈值（0~32768），需实测
    vad_speech_min_sec: float = 0.5         # 最短有效语音，过滤误触
    vad_silence_end_sec: float = 1.2        # 静音多久判定说话结束
    tts_mute_tail_sec: float = 0.2          # TTS 结束后 ASR 额外静默（消余音）

    # ── 全局播报频率上限（硬限制）────────────────────────────────────
    global_tts_min_interval: float = 2.0    # 任意两次被动播报之间至少间隔（秒）


# 全局单例
_config: Config | None = None


def get_config() -> Config:
    global _config
    if _config is None:
        _config = Config()
    return _config
