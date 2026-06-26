"""按配置路由到 Claude VLM 或本地 mock VLM。"""

from __future__ import annotations
import logging
import os
from typing import TYPE_CHECKING

from PIL import Image

from backend.config import Config
from backend.fast.event import GameEvent
from backend.slow import vlm_client
from backend.slow.vlm_mock import call_vlm_mock

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


def vlm_mock_enabled(cfg: Config) -> bool:
    env = os.getenv("VLM_MOCK")
    if env is not None:
        return env.strip().lower() in ("1", "true", "yes", "on")
    if cfg.vlm_mock:
        return True
    key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    return not key


async def call_vlm(
    event: GameEvent,
    frame: Image.Image,
    ctx_summary: str,
    last_fast_text: str,
    user_question: str = "",
    conversation_history: list[dict] | None = None,
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 120,
    cfg: Config | None = None,
) -> str:
    from backend.config import get_config
    cfg = cfg or get_config()

    if vlm_mock_enabled(cfg):
        logger.info("VLM mock mode: %s", event.type.value)
        return await call_vlm_mock(
            event,
            user_question,
            delay_sec=cfg.vlm_mock_delay_sec,
        )

    return await vlm_client.call_vlm(
        event=event,
        frame=frame,
        ctx_summary=ctx_summary,
        last_fast_text=last_fast_text,
        user_question=user_question,
        conversation_history=conversation_history,
        model=model,
        max_tokens=max_tokens,
    )
