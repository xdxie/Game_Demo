"""快系统 TTS 优先级分档（数值越小优先级越高）。"""

from __future__ import annotations

from enum import IntEnum


class FastPriority(IntEnum):
    SPELL = 0       # P0: RT/LT 修饰键窗口 + 面部/十字键 combo
    BUTTON = 1      # P1: 其它按键边沿
    INTENT = 2      # P2: 攻击窗口 / 持续危险
    DIRECTION = 3   # P3: 走位 / 带方向闪避 / 动作突变
