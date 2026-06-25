"""
GameEvent 和 EventType 数据结构定义。
快慢系统之间通过 GameEvent 传递信息。
"""

from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.nitrogen.parser import PerceptionSignal


class EventType(Enum):
    # ── 快系统事件（来自 NitroGen 动作过滤）──────────────────────────
    SUDDEN_DODGE         = "sudden_dodge"         # 突发闪避（高置信 + 突变）
    ATTACK_WINDOW        = "attack_window"        # 攻击窗口开启（从防御切攻击）
    SUSTAINED_DANGER     = "sustained_danger"     # 持续危险（长时间高置信 DODGE）
    MOVEMENT_SHIFT       = "movement_shift"       # 移动方向突变

    # ── 慢系统触发事件 ────────────────────────────────────────────────
    PATTERN_COMPLETED    = "pattern_completed"    # 一段连续操作结束（NitroGen WAIT）
    SUSTAINED_DIVERGENCE = "sustained_divergence" # 用户与 AI 长时间背离（预留）
    USER_QUESTION        = "user_question"        # 用户主动提问


@dataclass
class GameEvent:
    type: EventType
    timestamp: float               # 视频时间轴时间（秒），是真值，非系统时间
    perception: "PerceptionSignal"
    trigger_fast: bool             # 是否触发快通道
    trigger_slow: bool             # 是否触发慢通道
    user_text: str = ""            # 用户提问内容（USER_QUESTION 时使用）
