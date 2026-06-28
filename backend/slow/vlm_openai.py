"""OpenAI 兼容 Chat Completions（yunwu.ai / Gemini 等）。"""

from __future__ import annotations
import asyncio
import base64
import concurrent.futures
import io
import json as _json
import logging
import time
import traceback
from typing import Any, AsyncGenerator

import httpx
import requests as req
from PIL import Image

_SENTENCE_ENDS = frozenset('。！？\n')

from backend.config import Config, get_config
from backend.fast.event import GameEvent
from backend.slow.vlm_prompt import system_prompt, build_user_text

logger = logging.getLogger(__name__)

_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="vlm")


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


def _call_sync(
    url: str,
    payload: dict,
    headers: dict,
    timeout: float,
    event_type: str,
) -> str:
    t0 = time.time()
    logger.info("VLM openai calling → %s (timeout=%.0fs)", url, timeout)
    try:
        resp = req.post(url, json=payload, headers=headers, timeout=timeout)
    except Exception as e:
        logger.error("VLM openai requests.post failed: %s: %s", type(e).__name__, e)
        raise
    elapsed = time.time() - t0
    if resp.status_code >= 400:
        raise RuntimeError(f"VLM API {resp.status_code}: {resp.text[:500]}")
    data = resp.json()
    choice = data["choices"][0]
    text = choice["message"]["content"]
    if isinstance(text, list):
        text = "".join(
            p.get("text", "") for p in text if isinstance(p, dict)
        )
    text = str(text).strip()
    logger.info("VLM openai [%s] %.1fs: %s", event_type, elapsed, text[:80])
    return text


async def call_vlm_openai(
    event: GameEvent,
    frame: Image.Image,
    ctx_summary: str,
    last_fast_text: str,
    actions_timeline_text: str,
    user_question: str = "",
    conversation_history: list[dict] | None = None,
    slow_spoken: list[str] | None = None,
    cfg: Config | None = None,
    include_nitrogen: bool = False,
) -> str:
    cfg = cfg or get_config()
    api_key = (cfg.vlm_api_key or "").strip()
    if not api_key:
        raise RuntimeError("VLM_API_KEY 未配置")

    base = (cfg.vlm_api_base or "https://yunwu.ai/v1").rstrip("/")
    url = f"{base}/chat/completions"

    logger.info("VLM openai [%s] building request ...", event.type.value)

    try:
        user_text = build_user_text(
            event, ctx_summary, last_fast_text, actions_timeline_text, user_question,
            include_nitrogen=include_nitrogen,
            slow_spoken=slow_spoken,
        )
    except Exception as e:
        logger.error("VLM openai build_user_text failed: %s\n%s", e, traceback.format_exc())
        raise

    try:
        messages = _openai_messages(user_text, frame, conversation_history)
    except Exception as e:
        logger.error("VLM openai _openai_messages failed: %s\n%s", e, traceback.format_exc())
        raise

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

    logger.info("VLM openai [%s] dispatching to thread pool ...", event.type.value)

    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(
            _executor,
            _call_sync, url, payload, headers, cfg.vlm_api_timeout_sec, event.type.value,
        )
    except Exception as e:
        logger.error("VLM openai executor failed: %s: %s\n%s", type(e).__name__, e, traceback.format_exc())
        raise

    return result


async def call_vlm_openai_streaming(
    event: GameEvent,
    frame: Image.Image,
    ctx_summary: str,
    last_fast_text: str,
    actions_timeline_text: str,
    user_question: str = "",
    conversation_history: list[dict] | None = None,
    slow_spoken: list[str] | None = None,
    cfg: Config | None = None,
    include_nitrogen: bool = False,
) -> AsyncGenerator[str, None]:
    """流式调用 VLM，按句子边界 yield 文本段，降低首包时延。"""
    cfg = cfg or get_config()
    api_key = (cfg.vlm_api_key or "").strip()
    if not api_key:
        raise RuntimeError("VLM_API_KEY 未配置")

    base = (cfg.vlm_api_base or "https://yunwu.ai/v1").rstrip("/")
    url = f"{base}/chat/completions"

    user_text = build_user_text(
        event, ctx_summary, last_fast_text, actions_timeline_text, user_question,
        include_nitrogen=include_nitrogen, slow_spoken=slow_spoken,
    )
    messages = _openai_messages(user_text, frame, conversation_history)
    payload = {
        "model": cfg.vlm_model,
        "max_tokens": cfg.vlm_max_tokens,
        "stream": True,
        "messages": [
            {"role": "system", "content": system_prompt(include_nitrogen)},
            *messages,
        ],
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    logger.info("VLM openai [%s] streaming → %s", event.type.value, url)
    t0 = time.time()
    buf = ""

    async with httpx.AsyncClient(timeout=cfg.vlm_api_timeout_sec) as client:
        async with client.stream("POST", url, json=payload, headers=headers) as resp:
            if resp.status_code >= 400:
                body = await resp.aread()
                raise RuntimeError(f"VLM API {resp.status_code}: {body[:200]}")
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data.strip() == "[DONE]":
                    break
                try:
                    token = _json.loads(data)["choices"][0]["delta"].get("content") or ""
                except Exception:
                    continue
                buf += token
                # 只在句末符后面已有更多内容时才切句（避免末尾提前切断）
                while len(buf) > 1:
                    idx = next((i for i, c in enumerate(buf[:-1]) if c in _SENTENCE_ENDS), -1)
                    if idx < 0:
                        break
                    sentence = buf[:idx + 1].strip()
                    buf = buf[idx + 1:]
                    if sentence:
                        yield sentence

    if buf.strip():
        yield buf.strip()

    logger.info("VLM openai [%s] streaming done %.1fs", event.type.value, time.time() - t0)


def selftest(cfg: Config | None = None) -> bool:
    """Startup self-test: verify VLM API is reachable."""
    cfg = cfg or get_config()
    api_key = (cfg.vlm_api_key or "").strip()
    if not api_key:
        logger.warning("VLM selftest skip: no API key")
        return False

    base = (cfg.vlm_api_base or "https://yunwu.ai/v1").rstrip("/")
    url = f"{base}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": cfg.vlm_model,
        "max_tokens": 20,
        "messages": [
            {"role": "user", "content": "Say OK"},
        ],
    }
    try:
        t0 = time.time()
        resp = req.post(url, json=payload, headers=headers, timeout=15)
        elapsed = time.time() - t0
        if resp.status_code >= 400:
            logger.error("VLM selftest FAIL: HTTP %d: %s", resp.status_code, resp.text[:200])
            return False
        data = resp.json()
        text = data["choices"][0]["message"]["content"]
        logger.info("VLM selftest OK (%.1fs): %s", elapsed, str(text)[:60])
        return True
    except Exception as e:
        logger.error("VLM selftest FAIL: %s: %s", type(e).__name__, e)
        return False
