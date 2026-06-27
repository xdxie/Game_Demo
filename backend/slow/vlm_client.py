"""
VLM 客户端：调用 Claude API，生成游戏语音教练回答。

Prompt 设计由 4 号负责迭代，此处是初版。
调优方向（4号）：
- 回答是否太长 / 太泛 / 没用到 NitroGen 信号 / 重复了快通道内容
- 覆盖场景：策略类（"这段该怎么打"）、状态类（"现在什么情况"）、
           评价类（"刚才那样对吗"）、PATTERN_COMPLETED 自动总结
- 调整 vlm_max_tokens，确认字数限制下回答仍然有意义
"""

from __future__ import annotations
import base64
import io
import logging

from PIL import Image

from backend.fast.event import GameEvent
from backend.slow.vlm_prompt import system_prompt, build_user_text


def _pil_to_base64(img: Image.Image, max_size: int = 512) -> str:
    """PIL Image → base64 JPEG 字符串（压缩到 max_size 避免 API 超限）"""
    img = img.copy()
    img.thumbnail((max_size, max_size), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()


async def call_vlm(
    event: GameEvent,
    frame: Image.Image,
    ctx_summary: str,
    last_fast_text: str,
    actions_timeline_text: str = "",
    user_question: str = "",
    conversation_history: list[dict] | None = None,
    slow_spoken: list[str] | None = None,
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 120,
    include_nitrogen: bool = False,
) -> str:
    """调用 Claude API，返回语音教练回答文本。"""
    if conversation_history is None:
        conversation_history = []

    user_text = build_user_text(
        event, ctx_summary, last_fast_text, actions_timeline_text, user_question,
        include_nitrogen=include_nitrogen,
        slow_spoken=slow_spoken,
    )
    img_b64 = _pil_to_base64(frame)
    current_turn = {
        "role": "user",
        "content": [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": img_b64,
                },
            },
            {"type": "text", "text": user_text},
        ],
    }
    messages = conversation_history + [current_turn]

    # ── 调用 API ─────────────────────────────────────────────────────
    import anthropic
    client = anthropic.AsyncAnthropic()
    response = await client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system_prompt(include_nitrogen),
        messages=messages,
    )

    text = response.content[0].text.strip()
    logger.info("VLM response [%s]: %s", event.type.value, text[:60])
    return text
