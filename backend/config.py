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
    tts_voice: str = "zh-CN-YunxiNeural"    # edge-tts 声音（3号选音色）
    tts_rate: str = "+20%"                  # 语速（+20% 偏快，游戏场景）
    tts_inter_utterance_gap: float = 0.8    # 两条语音之间的间隔（秒）
    tts_done_fallback_margin: float = 1.0     # 前端未回 tts_done 时的额外宽限（秒）
    fast_hint_expire_sec: float = 2.0       # 快提示超时丢弃（秒）

    # ── ASR（5号负责调优）─────────────────────────────────────────────
    whisper_model: str = "base"
    whisper_language: str = "zh"
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
