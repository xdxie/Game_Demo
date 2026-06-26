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
from typing import TYPE_CHECKING

import anthropic
from PIL import Image

from backend.fast.event import EventType, GameEvent
from backend.nitrogen.parser import PerceptionSignal

if TYPE_CHECKING:
    from backend.slow.context_buffer import ContextBuffer

logger = logging.getLogger(__name__)


# ── System Prompt（4号迭代）──────────────────────────────────────────
SYSTEM_PROMPT = """你是一个游戏语音教练，正在实时陪伴玩家观看游戏视频录像。
旁边有一个 AI 系统（NitroGen）在分析每一帧画面，给出它认为的最优动作。

你的职责：
- 基于当前画面和 NitroGen 感知信号，给出简短、有价值的建议或回答
- 1~2 句话，不超过 40 字
- 口语化，像有经验的老玩家在旁指导，有时鼓励，有时提醒
- 如果快通道刚才已经说过某个方向提示，不要重复，深入一层

约束：
- 不用列表，不用 Markdown，不用标点符号堆砌
- 信息不足时给出最合理推断，不要说"我不确定"
- 不超过 40 字"""


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
    user_question: str = "",
    conversation_history: list[dict] | None = None,
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 120,
) -> str:
    """
    调用 Claude API，返回语音教练回答文本。

    Args:
        event: 触发该 VLM 调用的 GameEvent
        frame: 当前视频帧（PIL Image，256x256）
        ctx_summary: ContextBuffer.summarize() 的输出
        last_fast_text: FastHistory.get_recent_summary() 的输出
        user_question: 用户提问文本（USER_QUESTION 事件时非空）
        conversation_history: ConversationHistory.to_messages() 的输出
        model: Claude 模型 ID
        max_tokens: 最大输出 token 数
    """
    signal = event.perception

    if conversation_history is None:
        conversation_history = []

    # ── 构造 user message ────────────────────────────────────────────
    if user_question:
        task_desc = f"玩家提问：{user_question}"
        guidance  = "直接回答玩家问题，结合当前画面和 NitroGen 感知信号。"
    elif event.type == EventType.PATTERN_COMPLETED:
        task_desc = "触发原因：玩家刚结束一段操作"
        guidance  = "总结刚才那段操作的情况，给一句最有价值的点评或建议。"
    elif event.type == EventType.ATTACK_WINDOW:
        task_desc = "触发原因：AI 检测到攻击窗口"
        guidance  = "说明这个时机为什么可以进攻，以及注意什么。"
    else:
        task_desc = f"触发原因：{event.type.value}"
        guidance  = "给出当前局面下最有价值的一句建议，不要重复快通道刚说的内容。"

    user_text = (
        f"{ctx_summary}\n\n"
        f"NitroGen 操控量（简化）：\n"
        f"- 转向 steer：{signal.steer:+.2f}（-1 左，+1 右）\n"
        f"- 油门 throttle：{signal.throttle}\n"
        f"- 刹车 brake：{signal.brake}\n"
        f"- 主导意图：{signal.primary_intent}（置信度 {signal.confidence:.0%}）\n"
        f"- 方向：{signal.move_direction or '无'}\n"
        f"- 未来预测：{'→'.join(signal.horizon_sequence)}\n\n"
        f"快通道刚才已播报：\"{last_fast_text}\"\n\n"
        f"{task_desc}\n"
        f"{guidance}"
    )

    # ── 构造 messages ────────────────────────────────────────────────
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
    client = anthropic.AsyncAnthropic()
    response = await client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=SYSTEM_PROMPT,
        messages=messages,
    )

    text = response.content[0].text.strip()
    logger.info("VLM response [%s]: %s", event.type.value, text[:60])
    return text
