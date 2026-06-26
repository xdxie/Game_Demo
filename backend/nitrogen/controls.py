"""从简化操控量构造 PerceptionSignal（mock NitroGen / VLM 输入）。"""

from __future__ import annotations

from backend.nitrogen.parser import PerceptionSignal


def signal_from_controls(
    steer: float,
    throttle: int,
    brake: int,
    confidence: float = 0.8,
) -> PerceptionSignal:
    """
    steer: [-1, 1] 左右；throttle/brake: 0/1。
    同时推导 primary_intent 供快系统 ActionFilter 使用。
    """
    steer = max(-1.0, min(1.0, float(steer)))
    throttle = 1 if throttle else 0
    brake = 1 if brake else 0

    if brake:
        intent = "DODGE"
        direction = None
        horizon = ["BRAKE×4", "WAIT×4"]
    elif throttle and steer < -0.25:
        intent = "NAVIGATE"
        direction = "LEFT"
        horizon = [f"STEER_L×6", "THROTTLE×4"]
    elif throttle and steer > 0.25:
        intent = "NAVIGATE"
        direction = "RIGHT"
        horizon = [f"STEER_R×6", "THROTTLE×4"]
    elif throttle:
        intent = "NAVIGATE"
        direction = "FORWARD"
        horizon = ["THROTTLE×8"]
    else:
        intent = "WAIT"
        direction = None
        horizon = ["COAST×8"]

    mag = abs(steer) if throttle else 0.0

    return PerceptionSignal(
        primary_intent=intent,
        confidence=confidence,
        move_direction=direction,
        move_magnitude=mag,
        horizon_sequence=horizon,
        raw_joystick_mag=mag,
        steer=steer,
        throttle=throttle,
        brake=brake,
    )
