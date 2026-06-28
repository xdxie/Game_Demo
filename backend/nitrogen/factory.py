"""按配置创建 NitroGen 客户端：mock / fast_api HTTP / ZMQ。"""

from __future__ import annotations
import logging
import os

from backend.config import Config
from backend.nitrogen.mock_client import MockNitroGenClient

logger = logging.getLogger(__name__)


def nitrogen_mock_enabled(cfg: Config) -> bool:
    env = os.getenv("NITROGEN_MOCK")
    if env is not None:
        return env.strip().lower() in ("1", "true", "yes", "on")
    return cfg.nitrogen_mock


def nitrogen_backend(cfg: Config) -> str:
    if nitrogen_mock_enabled(cfg):
        return "mock"
    env = os.getenv("NITROGEN_BACKEND", "").strip().lower()
    if env:
        return env
    return (cfg.nitrogen_backend or "zmq").strip().lower()


def nitrogen_mode_label(cfg: Config) -> str:
    """探针/日志用：mock | fast_api | zmq。"""
    return nitrogen_backend(cfg)


def nitrogen_analysis_fps(cfg: Config) -> float:
    """主分析循环与推帧速率上限。"""
    if nitrogen_backend(cfg) == "fast_api":
        return float(os.getenv("NITROGEN_FAST_API_FPS", cfg.nitrogen_fast_api_fps))
    return cfg.nitrogen_target_fps


def create_nitrogen_client(cfg: Config):
    backend = nitrogen_backend(cfg)
    if backend == "mock":
        return MockNitroGenClient()

    if backend == "fast_api":
        from backend.nitrogen.fast_api_client import FastApiNitroGenClient
        url = os.getenv("NITROGEN_FAST_API_URL", cfg.nitrogen_fast_api_url)
        fps = float(os.getenv("NITROGEN_FAST_API_FPS", cfg.nitrogen_fast_api_fps))
        timeout = float(
            os.getenv("NITROGEN_FAST_API_TIMEOUT", cfg.nitrogen_fast_api_timeout_sec)
        )
        reset_env = os.getenv("NITROGEN_FAST_API_RESET_ON_START")
        reset_on_start = (
            reset_env.strip().lower() in ("1", "true", "yes", "on")
            if reset_env is not None
            else cfg.nitrogen_fast_api_reset_on_start
        )
        return FastApiNitroGenClient(
            base_url=url,
            target_fps=fps,
            timeout_sec=timeout,
            reset_on_start=reset_on_start,
            dump_path=cfg.nitrogen_dump_path,
            dump_pretty=cfg.nitrogen_dump_pretty,
        )

    from backend.nitrogen.client import NitroGenClient
    addr = os.getenv("NITROGEN_SERVER", cfg.nitrogen_server)
    return NitroGenClient(server_addr=addr)
