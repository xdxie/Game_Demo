"""按配置路由 VLM：OpenAI 兼容网关 / Claude / mock。"""

from __future__ import annotations
import logging
import os

from PIL import Image

from backend.config import Config, get_config
from backend.fast.event import GameEvent
from backend.slow import vlm_client
from backend.slow.vlm_mock import call_vlm_mock
from backend.slow.vlm_openai import call_vlm_openai

logger = logging.getLogger(__name__)


def _resolve_api_key(cfg: Config) -> str:
    return (
        os.getenv("VLM_API_KEY", "").strip()
        or os.getenv("YUNWU_API_KEY", "").strip()
        or (cfg.vlm_api_key or "").strip()
    )


def vlm_provider(cfg: Config | None = None) -> str:
    """返回 mock | openai | anthropic"""
    cfg = cfg or get_config()
    env_mock = os.getenv("VLM_MOCK")
    if env_mock is not None:
        if env_mock.strip().lower() in ("1", "true", "yes", "on"):
            return "mock"
        if _resolve_api_key(cfg) and cfg.vlm_provider == "openai":
            return "openai"

    if vlm_mock_enabled(cfg):
        return "mock"
    if cfg.vlm_provider == "openai" and _resolve_api_key(cfg):
        return "openai"
    if os.getenv("ANTHROPIC_API_KEY", "").strip():
        return "anthropic"
    return "mock"


def vlm_mock_enabled(cfg: Config) -> bool:
    env = os.getenv("VLM_MOCK")
    if env is not None:
        return env.strip().lower() in ("1", "true", "yes", "on")
    if _resolve_api_key(cfg) and cfg.vlm_provider == "openai":
        return False
    if cfg.vlm_mock:
        return True
    return not os.getenv("ANTHROPIC_API_KEY", "").strip()


async def call_vlm(
    event: GameEvent,
    frame: Image.Image,
    ctx_summary: str,
    last_fast_text: str,
    actions_timeline_text: str = "",
    user_question: str = "",
    conversation_history: list[dict] | None = None,
    model: str | None = None,
    max_tokens: int | None = None,
    cfg: Config | None = None,
    include_nitrogen: bool | None = None,
) -> str:
    cfg = cfg or get_config()
    provider = vlm_provider(cfg)
    nitrogen = (
        include_nitrogen
        if include_nitrogen is not None
        else cfg.vlm_nitrogen_input
    )
    logger.info(
        "VLM route: provider=%s model=%s event=%s",
        provider, cfg.vlm_model, event.type.value,
    )

    if provider == "mock":
        logger.info("VLM mock mode: %s", event.type.value)
        return await call_vlm_mock(
            event,
            user_question,
            actions_timeline_text=actions_timeline_text,
            delay_sec=cfg.vlm_mock_delay_sec,
            include_nitrogen=nitrogen,
        )

    if provider == "openai":
        return await call_vlm_openai(
            event=event,
            frame=frame,
            ctx_summary=ctx_summary,
            last_fast_text=last_fast_text,
            actions_timeline_text=actions_timeline_text,
            user_question=user_question,
            conversation_history=conversation_history,
            cfg=cfg,
            include_nitrogen=nitrogen,
        )

    return await vlm_client.call_vlm(
        event=event,
        frame=frame,
        ctx_summary=ctx_summary,
        last_fast_text=last_fast_text,
        actions_timeline_text=actions_timeline_text,
        user_question=user_question,
        conversation_history=conversation_history,
        model=model or cfg.vlm_model,
        max_tokens=max_tokens or cfg.vlm_max_tokens,
        include_nitrogen=nitrogen,
    )
