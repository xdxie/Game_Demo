"""VLM 模拟：无 API Key 时生成短回复。"""

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
    actions_timeline_text: str = "",
    delay_sec: float = 0.35,
    include_nitrogen: bool = False,
) -> str:
    """模拟 VLM：短暂延迟后返回规则回复。"""
    await asyncio.sleep(delay_sec)

    if event.type == EventType.USER_QUESTION and user_question:
        if include_nitrogen:
            signal = event.perception
            ctrl = _control_hint(signal)
            base = f"收到：{user_question[:16]}。当前{ctrl}。"
            if actions_timeline_text:
                return base + " 已参考动作时间线。"
            return base + " 保持节奏。"
        return f"关于「{user_question[:20]}」，请先看画面再决定下一步。"

    if include_nitrogen:
        signal = event.perception
        if signal.brake:
            return "时间线显示有刹车点，可以再提前一点。"
        if signal.throttle and abs(signal.steer) > 0.3:
            side = "左" if signal.steer < 0 else "右"
            return f"向{side}给油，注意看时间线里的转向段。"
        if signal.throttle:
            return "直线油门段，保持节奏。"

    return "先看清楚画面局势，再决定下一步操作。"
