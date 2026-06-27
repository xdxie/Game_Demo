"""
全局配置。所有可调参数集中于此，每个参数都标注了负责调优的角色。
"""

from __future__ import annotations
import os
from dataclasses import dataclass, field


@dataclass
class Config:
    # ── NitroGen 连接 ─────────────────────────────────────────────────
    nitrogen_server: str = "tcp://localhost:5555"
    nitrogen_target_fps: float = 10.0    # 向 NitroGen 发送的帧率（推理约200ms/chunk）
    nitrogen_mock: bool = False          # True = 模拟 perception，False = 连接真实 NitroGen
    nitrogen_backend: str = "fast_api"   # mock | fast_api | zmq
    nitrogen_fast_api_url: str = "http://localhost:18000"
    nitrogen_fast_api_timeout_sec: float = 60.0
    nitrogen_fast_api_reset_on_start: bool = True
    nitrogen_fast_api_fps: float = 2.5   # 远端推理较慢，默认低于 10fps
    nitrogen_ssh_tunnel: bool = True     # 启动时自动 ssh -L（见 NITROGEN_SSH_*）
    nitrogen_ssh_host: str = "connect.bjb1.seetacloud.com"
    nitrogen_ssh_port: int = 18037
    nitrogen_ssh_user: str = "root"
    nitrogen_ssh_remote_port: int = 8000
    nitrogen_ssh_key: str = ""           # 可选私钥路径
    nitrogen_ssh_password: str = "qmkXaxUs99f4"      # SSH 密码
    fast_tts_enabled: bool = True      # mock 模式下默认关闭，见 _apply_env

    # ── 快系统：动作过滤阈值（2号负责调优）────────────────────────────
    fast_trigger_confidence: float = 0.75   # 快通道触发置信度下限
    sustained_danger_sec: float = 3.0       # DODGE 持续多久触发 SUSTAINED_DANGER

    # 各事件类型冷却时间（秒）
    cooldowns: dict = field(default_factory=lambda: {
        "sudden_dodge":      5.0,
        "attack_window":     6.0,
        "sustained_danger":  10.0,
        "movement_shift":   15.0,
        "pattern_completed": 8.0,
    })

    # ── 慢系统：VLM（4号负责调优）────────────────────────────────────
    vlm_provider: str = "openai"          # openai | anthropic | mock
    vlm_api_base: str = "https://yunwu.ai/v1"
    vlm_api_key: str = "sk-rDl2CSNC6PhNFcfnI2jGH7UGnORAhSmgXkgBfAq7cAz2rqKS"
    vlm_model: str = "gemini-3.1-flash-lite:stable"
    vlm_api_timeout_sec: float = 60.0
    vlm_max_tokens: int = 120
    vlm_mock: bool = False                # 无 Key 时 factory 自动降级 mock
    vlm_mock_delay_sec: float = 0.35
    actions_sample_interval_sec: float = 2.0
    context_window_sec: float = 15.0    # 上下文缓冲区时间窗口
    slow_max_queue_age: float = 8.0     # 慢系统 TTS 结果有效期（秒）
    vlm_dedup_sec: float = 5.0          # 同类事件 VLM 去重窗口
    vlm_nitrogen_input: bool = False    # 暂不把 NitroGen 操控/时间线注入 VLM

    # ── TTS（3号负责调优）─────────────────────────────────────────────
    # tts_engine: volcengine | edge-tts
    tts_engine: str = "volcengine"
    volc_api_key: str = "d1dca442-e60a-49a6-b296-fa6ae31fd04e"
    volc_speaker: str = "zh_female_cancan_uranus_bigtts"
    volc_speed_ratio: float = 1.5
    tts_voice: str = "zh-CN-XiaoxiaoNeural"  # edge-tts 声音（3号选音色）
    tts_rate: str = "+50%"                  # 语速（+50% 快节奏，游戏场景）
    tts_inter_utterance_gap: float = 0.8    # 两条语音之间的间隔（秒）
    tts_user_inter_gap: float = 0.15        # 用户问答播报后的间隔（秒）
    tts_done_fallback_margin: float = 1.0     # 前端未回 tts_done 时的额外宽限（秒）
    tts_synthesis_timeout_sec: float = 15.0   # edge-tts 合成超时（秒），防止 ASR 长期 muted
    fast_hint_expire_sec: float = 2.0       # 快提示超时丢弃（秒）

    # ── ASR（5号负责调优）─────────────────────────────────────────────
    # asr_engine: faster-whisper | openai-whisper
    asr_engine: str = "faster-whisper"
    asr_device: str = "cuda"                # auto | cuda | cpu（仅 faster-whisper）
    whisper_model: str = "base"
    whisper_language: str = "zh"
    vad_silence_threshold: int = 350         # 振幅门限（需在真实环境下校准）
    vad_speech_min_sec: float = 0.35        # 最短有效语音（秒）
    vad_silence_end_sec: float = 0.9        # 长句静音多久判定说话结束
    vad_silence_end_short_sec: float = 0.6  # 短句静音判定（自适应 VAD）
    vad_adaptive_boundary_sec: float = 1.0  # 短句/长句分界（秒）
    vad_max_speech_sec: float = 8.0         # 最长连续语音，超时强制送识别（防游戏底噪卡死）
    tts_mute_tail_sec: float = 0.2          # TTS 结束后 ASR 额外静默（消余音）
    barge_in_enabled: bool = True           # TTS 播报时检测用户说话并打断
    barge_in_threshold_mult: float = 2.0   # 打断阈值 = 静音阈值 × 此系数（防视频串音）

    # ── 全局播报频率上限（硬限制）────────────────────────────────────
    global_tts_min_interval: float = 8.0    # 任意两次被动播报之间至少间隔（秒）


# 全局单例
_config: Config | None = None
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def reload_config_from_env() -> Config:
    """重新从环境变量刷新配置（load_dotenv 之后调用）。"""
    global _config
    if _config is None:
        _config = Config()
    _apply_env(_config)
    if _config.vlm_api_key and os.getenv("VLM_MOCK") is None:
        _config.vlm_mock = False
    return _config


def _env_bool(name: str) -> bool | None:
    raw = os.getenv(name)
    if raw is None:
        return None
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _apply_env(cfg: Config) -> None:
    """从 .env / 环境变量覆盖配置（load_dotenv 后调用）。"""
    if v := os.getenv("NITROGEN_SERVER"):
        cfg.nitrogen_server = v
    mock = _env_bool("NITROGEN_MOCK")
    if mock is not None:
        cfg.nitrogen_mock = mock

    if v := os.getenv("NITROGEN_BACKEND"):
        cfg.nitrogen_backend = v.strip().lower()
    if v := os.getenv("NITROGEN_FAST_API_URL"):
        cfg.nitrogen_fast_api_url = v.strip()
    if v := os.getenv("NITROGEN_FAST_API_TIMEOUT"):
        cfg.nitrogen_fast_api_timeout_sec = float(v)
    if v := os.getenv("NITROGEN_FAST_API_FPS"):
        cfg.nitrogen_fast_api_fps = float(v)
    reset = _env_bool("NITROGEN_FAST_API_RESET_ON_START")
    if reset is not None:
        cfg.nitrogen_fast_api_reset_on_start = reset

    tunnel = _env_bool("NITROGEN_SSH_TUNNEL")
    if tunnel is not None:
        cfg.nitrogen_ssh_tunnel = tunnel
    if v := os.getenv("NITROGEN_SSH_HOST"):
        cfg.nitrogen_ssh_host = v.strip()
    if v := os.getenv("NITROGEN_SSH_PORT"):
        cfg.nitrogen_ssh_port = int(v)
    if v := os.getenv("NITROGEN_SSH_USER"):
        cfg.nitrogen_ssh_user = v.strip()
    if v := os.getenv("NITROGEN_SSH_REMOTE_PORT"):
        cfg.nitrogen_ssh_remote_port = int(v)
    if v := os.getenv("NITROGEN_SSH_KEY"):
        cfg.nitrogen_ssh_key = v.strip()
    if v := os.getenv("NITROGEN_SSH_PASSWORD"):
        cfg.nitrogen_ssh_password = v

    if v := os.getenv("VLM_API_KEY"):
        cfg.vlm_api_key = v.strip()
    if v := os.getenv("VLM_API_BASE"):
        cfg.vlm_api_base = v.strip()
    if v := os.getenv("VLM_MODEL"):
        cfg.vlm_model = v.strip()
    if v := os.getenv("VLM_PROVIDER"):
        cfg.vlm_provider = v.strip()
    mock_vlm = _env_bool("VLM_MOCK")
    if mock_vlm is not None:
        cfg.vlm_mock = mock_vlm

    fast_tts = _env_bool("FAST_TTS")
    if fast_tts is not None:
        cfg.fast_tts_enabled = fast_tts

    barge = _env_bool("BARGE_IN")
    if barge is not None:
        cfg.barge_in_enabled = barge

    nit_in = _env_bool("VLM_NITROGEN_INPUT")
    if nit_in is not None:
        cfg.vlm_nitrogen_input = nit_in

    if v := os.getenv("TTS_ENGINE"):
        cfg.tts_engine = v.strip().lower()
    if v := os.getenv("VOLC_API_KEY"):
        cfg.volc_api_key = v.strip()
    if v := os.getenv("VOLC_SPEAKER"):
        cfg.volc_speaker = v.strip()
    if v := os.getenv("VOLC_SPEED_RATIO"):
        cfg.volc_speed_ratio = float(v)
    if v := os.getenv("TTS_VOICE"):
        cfg.tts_voice = v.strip()
    if v := os.getenv("TTS_RATE"):
        cfg.tts_rate = v.strip()

    if v := os.getenv("ASR_ENGINE"):
        cfg.asr_engine = v.strip().lower()
    if v := os.getenv("ASR_DEVICE"):
        cfg.asr_device = v.strip().lower()
    if v := os.getenv("WHISPER_MODEL"):
        cfg.whisper_model = v.strip()
    if v := os.getenv("WHISPER_LANGUAGE"):
        cfg.whisper_language = v.strip()
    if v := os.getenv("VAD_SILENCE_THRESHOLD"):
        cfg.vad_silence_threshold = int(v)
    if v := os.getenv("VAD_SPEECH_MIN_SEC"):
        cfg.vad_speech_min_sec = float(v)
    if v := os.getenv("VAD_SILENCE_END_SEC"):
        cfg.vad_silence_end_sec = float(v)


def get_config() -> Config:
    global _config
    if _config is None:
        _config = Config()
        _apply_env(_config)
        if _config.vlm_api_key and os.getenv("VLM_MOCK") is None:
            _config.vlm_mock = False
    return _config
