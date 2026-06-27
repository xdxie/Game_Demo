"""
快通道模板引擎：GameEvent → 短提示文本（≤8字）。

纯模板，不调用 LLM，延迟 <1ms。
文本内容由 3 号负责调音时同步审查（听起来是否自然、紧迫）。

调优说明（3号）：
- 每条文本朗读出来不超过 1.5 秒（edge-tts 默认语速）
- 有方向的事件优先使用带方向的模板
- 语气简洁有力，像老玩家在耳边提示，不要像机器播报
"""

from __future__ import annotations
import random
from typing import Callable

from backend.fast.event import EventType, GameEvent
from backend.nitrogen.parser import PerceptionSignal


# ── 方向中文映射 ──────────────────────────────────────────────────────
DIRECTION_ZH: dict[str | None, str] = {
    "LEFT":    "向左",
    "RIGHT":   "向右",
    "FORWARD": "向前",
    "BACK":    "向后",
    None:      "",
}

# ── 快通道模板表 ──────────────────────────────────────────────────────
# 每个事件类型对应一组模板 lambda，渲染时根据有无方向信息选择
# (有方向模板, 无方向模板)

_T = tuple  # (Callable, Callable) - 有方向模板 + 无方向模板

FAST_TEMPLATES: dict[EventType, _T] = {
    EventType.SUDDEN_DODGE: (
        lambda s: f"{DIRECTION_ZH[s.move_direction]}闪！",
        lambda s: "注意，快闪！",
    ),
    EventType.ATTACK_WINDOW: (
        lambda s: "有机会，打！",
        lambda s: "进攻！",
    ),
    EventType.SUSTAINED_DANGER: (
        lambda s: f"持续危险，{DIRECTION_ZH[s.move_direction] or ''}保持闪避",
        lambda s: "这段很危险，别停",
    ),
    EventType.MOVEMENT_SHIFT: (
        lambda s: f"往{DIRECTION_ZH[s.move_direction]}走",
        lambda s: "换个方向走",
    ),
}


def render_fast(event: GameEvent) -> str:
    """
    将 GameEvent 渲染为快通道提示文本。

    只有 trigger_fast=True 的事件才调用此函数。
    PATTERN_COMPLETED 不触发快通道，不在此处理。
    """
    templates = FAST_TEMPLATES.get(event.type)
    if templates is None:
        return "注意！"

    has_direction = event.perception.move_direction is not None
    fn = templates[0] if has_direction else templates[1]
    return fn(event.perception)
