"""
后台预热：选择视频后即可加载 Whisper + TTS 预缓存，缩短「开始分析」等待。
"""

from __future__ import annotations
import asyncio
import logging
import threading
from typing import Optional

from backend.config import Config, get_config
from backend.tts.engine import TTSEngine

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_status: str = "idle"  # idle | loading | ready | error
_error: Optional[str] = None
_whisper_model = None
_asr_engine_type: str = "openai-whisper"
_tts_cache: dict[str, bytes] = {}


def get_status() -> dict:
    with _lock:
        return {
            "status": _status,
            "whisper_ready": _whisper_model is not None,
            "tts_ready": bool(_tts_cache),
            "error": _error,
        }


def get_whisper_model(cfg: Config | None = None):
    """返回已预热的 Whisper 模型；若未预热则同步加载。"""
    global _whisper_model
    cfg = cfg or get_config()
    with _lock:
        if _whisper_model is not None:
            return _whisper_model
    return _load_whisper_blocking(cfg)


def get_asr_engine_type(cfg: Config | None = None) -> str:
    """返回已预热 ASR 模型对应的引擎类型。"""
    cfg = cfg or get_config()
    with _lock:
        if _whisper_model is not None:
            return _asr_engine_type
    _load_whisper_blocking(cfg)
    with _lock:
        return _asr_engine_type


def get_tts_cache() -> dict[str, bytes]:
    with _lock:
        return dict(_tts_cache)


def _load_whisper_blocking(cfg: Config):
    global _whisper_model, _asr_engine_type
    from backend.asr.handler import _load_model

    logger.info(
        "Warmup: loading ASR %s (%s/%s) ...",
        cfg.whisper_model, cfg.asr_engine, cfg.asr_device,
    )
    model, engine_type = _load_model(
        cfg.whisper_model, cfg.asr_engine, cfg.asr_device,
    )
    with _lock:
        _whisper_model = model
        _asr_engine_type = engine_type
    logger.info("Warmup: ASR ready (%s)", engine_type)
    return model


async def _warmup_async(cfg: Config):
    global _status, _error, _tts_cache
    loop = asyncio.get_running_loop()

    with _lock:
        if _status == "ready":
            return
        _status = "loading"
        _error = None

    try:
        engine = TTSEngine(
            engine=cfg.tts_engine,
            voice=cfg.tts_voice,
            rate=cfg.tts_rate,
            volc_api_key=cfg.volc_api_key,
            volc_speaker_fast=cfg.volc_speaker_fast,
            volc_speaker_slow=cfg.volc_speaker_slow,
            volc_speed_ratio_fast=cfg.volc_speed_ratio_fast,
            volc_speed_ratio_slow=cfg.volc_speed_ratio_slow,
        )
        tasks = []
        if _whisper_model is None:
            tasks.append(loop.run_in_executor(None, _load_whisper_blocking, cfg))
        tasks.append(loop.run_in_executor(None, engine.preload))
        await asyncio.gather(*tasks)

        with _lock:
            _tts_cache.clear()
            _tts_cache.update(engine._cache)
            _status = "ready"
        logger.info("Warmup: TTS cache %d phrases", len(_tts_cache))

    except Exception as e:
        logger.exception("Warmup failed")
        with _lock:
            _status = "error"
            _error = str(e)


async def start_background_warmup(cfg: Config | None = None) -> None:
    """在 FastAPI 请求中启动后台预热任务。"""
    cfg = cfg or get_config()
    st = get_status()
    if st["status"] in ("loading", "ready"):
        return
    asyncio.create_task(_warmup_async(cfg))


async def ensure_warmup(cfg: Config | None = None) -> None:
    """等待预热完成（GameSession.start 可调用）。"""
    cfg = cfg or get_config()
    st = get_status()
    if st["status"] == "ready":
        return
    if st["status"] != "loading":
        await start_background_warmup(cfg)
    while True:
        await asyncio.sleep(0.1)
        st = get_status()
        if st["status"] == "ready":
            return
        if st["status"] == "error":
            raise RuntimeError(st.get("error") or "warmup failed")
