"""OpenAI 兼容 Chat Completions（yunwu.ai / Gemini 等）。"""

from __future__ import annotations
import base64
import io
import logging
from typing import Any

import httpx
from PIL import Image

from backend.config import Config, get_config
from backend.fast.event import GameEvent
from backend.slow.vlm_prompt import system_prompt, build_user_text

logger = logging.getLogger(__name__)


def _pil_to_data_url(img: Image.Image, max_size: int = 512) -> str:
    img = img.copy()
    img.thumbnail((max_size, max_size), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"


def _openai_messages(
    user_text: str,
    frame: Image.Image,
    conversation_history: list[dict] | None,
) -> list[dict[str, Any]]:
    """将历史 + 当前多模态轮次转为 OpenAI messages。"""
    messages: list[dict[str, Any]] = []
    if conversation_history:
        for turn in conversation_history:
            role = turn.get("role", "user")
            content = turn.get("content", "")
            if isinstance(content, list):
                text_parts = [
                    p.get("text", "")
                    for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                ]
                content = "\n".join(text_parts)
            messages.append({"role": role, "content": content})

    messages.append({
        "role": "user",
        "content": [
            {"type": "text", "text": user_text},
            {
                "type": "image_url",
                "image_url": {"url": _pil_to_data_url(frame)},
            },
        ],
    })
    return messages


async def call_vlm_openai(
    event: GameEvent,
    frame: Image.Image,
    ctx_summary: str,
    last_fast_text: str,
    actions_timeline_text: str,
    user_question: str = "",
    conversation_history: list[dict] | None = None,
    cfg: Config | None = None,
    include_nitrogen: bool = False,
) -> str:
    cfg = cfg or get_config()
    api_key = (cfg.vlm_api_key or "").strip()
    if not api_key:
        raise RuntimeError("VLM_API_KEY 未配置")

    base = (cfg.vlm_api_base or "https://yunwu.ai/v1").rstrip("/")
    url = f"{base}/chat/completions"
    user_text = build_user_text(
        event, ctx_summary, last_fast_text, actions_timeline_text, user_question,
        include_nitrogen=include_nitrogen,
    )
    messages = _openai_messages(user_text, frame, conversation_history)

    payload = {
        "model": cfg.vlm_model,
        "max_tokens": cfg.vlm_max_tokens,
        "messages": [
            {"role": "system", "content": system_prompt(include_nitrogen)},
            *messages,
        ],
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    timeout = httpx.Timeout(cfg.vlm_api_timeout_sec, connect=30.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code >= 400:
            body = resp.text[:500]
            raise RuntimeError(f"VLM API {resp.status_code}: {body}")

        data = resp.json()
        choice = data["choices"][0]
        text = choice["message"]["content"]
        if isinstance(text, list):
            text = "".join(
                p.get("text", "") for p in text if isinstance(p, dict)
            )
        text = str(text).strip()
        logger.info("VLM openai [%s]: %s", event.type.value, text[:80])
        return text
