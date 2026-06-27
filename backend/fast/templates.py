"""
快通道模板引擎：GameEvent → 短提示文本（≤8字）。

纯模板，不调用 LLM，延迟 <1ms。
每个事件类型有多组模板变体，随机选取以避免重复感。
"""

from __future__ import annotations
import random
from typing import Callable

from backend.fast.event import EventType, GameEvent
from backend.nitrogen.parser import PerceptionSignal


# ── 方向中文映射 ──────────────────────────────────────────────────────
DIRECTION_ZH: dict[str | None, str] = {
    "LEFT":    "左",
    "RIGHT":   "右",
    "FORWARD": "前",
    "BACK":    "后",
    None:      "",
}

# ── 快通道模板表 ──────────────────────────────────────────────────────
# 每个事件类型: (有方向模板列表, 无方向模板列表)

FAST_TEMPLATES: dict[EventType, tuple[list[Callable], list[Callable]]] = {
    EventType.SUDDEN_DODGE: (
        [
            lambda s: f"往{DIRECTION_ZH[s.move_direction]}闪！",
            lambda s: f"{DIRECTION_ZH[s.move_direction]}边闪开！",
            lambda s: f"快往{DIRECTION_ZH[s.move_direction]}躲！",
            lambda s: f"小心！{DIRECTION_ZH[s.move_direction]}闪！",
        ],
        [
            lambda s: "注意，快闪！",
            lambda s: "闪开！",
            lambda s: "快躲！",
            lambda s: "小心！快闪避！",
        ],
    ),
    EventType.ATTACK_WINDOW: (
        [
            lambda s: "有机会，打！",
            lambda s: "现在打！",
            lambda s: "空档来了，出手！",
            lambda s: "机会！赶紧打！",
        ],
        [
            lambda s: "进攻！",
            lambda s: "上！打他！",
            lambda s: "出手！",
            lambda s: "机会来了，打！",
        ],
    ),
    EventType.SUSTAINED_DANGER: (
        [
            lambda s: f"危险，往{DIRECTION_ZH[s.move_direction]}跑！",
            lambda s: f"快撤！往{DIRECTION_ZH[s.move_direction]}拉开！",
            lambda s: f"太危险了，{DIRECTION_ZH[s.move_direction]}边走！",
        ],
        [
            lambda s: "危险！快拉开距离！",
            lambda s: "这段很危险，别停！",
            lambda s: "赶紧撤！太危险了！",
            lambda s: "快跑，别硬扛！",
        ],
    ),
    EventType.MOVEMENT_SHIFT: (
        [
            lambda s: f"往{DIRECTION_ZH[s.move_direction]}走",
            lambda s: f"往{DIRECTION_ZH[s.move_direction]}跑",
            lambda s: f"往{DIRECTION_ZH[s.move_direction]}移动",
            lambda s: f"{DIRECTION_ZH[s.move_direction]}边走",
            lambda s: f"靠{DIRECTION_ZH[s.move_direction]}走",
            lambda s: f"走{DIRECTION_ZH[s.move_direction]}边",
        ],
        [
            lambda s: "换个方向走",
            lambda s: "换个方位",
            lambda s: "挪一下位置",
            lambda s: "调整走位",
        ],
    ),
}


def render_fast(event: GameEvent) -> str:
    """
    将 GameEvent 渲染为快通道提示文本。
    从多个变体中随机选取，避免连续重复。
    """
    templates = FAST_TEMPLATES.get(event.type)
    if templates is None:
        return "注意！"

    has_direction = event.perception.move_direction is not None
    pool = templates[0] if has_direction else templates[1]
    fn = random.choice(pool)
    return fn(event.perception)
