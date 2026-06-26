"""VLM 模拟：无 Anthropic API 时根据操控量 + 用户语音生成短回复。"""

from __future__ import annotations
import asyncio
import logging

from backend.fast.event import EventType, GameEvent
from backend.nitrogen.parser import PerceptionSignal

logger = logging.getLogger(__name__)


def _control_hint(signal: PerceptionSignal) -> str:
    parts = [f"转向{signal.steer:+.2f}"]
    parts.append("油门" if signal.throttle else "油门关")
    parts.append("刹车" if signal.brake else "刹车关")
    return "，".join(parts)


async def call_vlm_mock(
    event: GameEvent,
    user_question: str = "",
    *,
    delay_sec: float = 0.35,
) -> str:
    """模拟 VLM：短暂延迟后返回基于操控量的规则回复（用于前端闭环）。"""
    await asyncio.sleep(delay_sec)
    signal = event.perception
    ctrl = _control_hint(signal)

    if event.type == EventType.USER_QUESTION and user_question:
        if signal.brake:
            return f"你问得好，当前在刹车，先稳住再操作。{ctrl}"
        if signal.throttle and signal.steer < -0.2:
            return f"现在在向左给油，可以准备回正。你说：{user_question[:12]}"
        if signal.throttle and signal.steer > 0.2:
            return f"向右给油中，注意别过度转向。你说：{user_question[:12]}"
        return f"收到：{user_question[:16]}。当前{ctrl}，保持节奏。"

    if signal.brake:
        return "刚才该减速了，刹车时机可以更早一点。"
    if signal.throttle and abs(signal.steer) > 0.3:
        side = "左" if signal.steer < 0 else "右"
        return f"持续向{side}给油，注意出弯回正。"
    if signal.throttle:
        return "直线油门可以，留意前方空隙。"
    return "这段可以滑行观察，别急。"
