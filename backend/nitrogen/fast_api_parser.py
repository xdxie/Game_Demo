"""
将 action_fast_system（远端 FastAPI /predict）JSON 解析为 PerceptionSignal。

扳机/摇杆为通用手柄语义，不假定赛车游戏。
"""

from __future__ import annotations

from backend.nitrogen.parser import (
    ATTACK_BUTTONS,
    DODGE_BUTTONS,
    PerceptionSignal,
)

_BTN_THRESHOLD = 0.5


def parse_predict_response(data: dict, btn_threshold: float = _BTN_THRESHOLD) -> PerceptionSignal:
    """解析 POST /predict 返回的 JSON。

    兼容两种格式：
    - 旧格式：action_summary.{left_stick_mean, right_stick_mean, trigger_means, buttons_avg_pressed}
    - 新格式（NitroGen 实际返回）：顶层 left_stick / right_stick / buttons_held
    """
    summary = data.get("action_summary") or {}
    left = data.get("left_stick") or summary.get("left_stick_mean") or [0.0, 0.0]
    right = data.get("right_stick") or summary.get("right_stick_mean") or [0.0, 0.0]
    pressed = list(data.get("buttons_held") or summary.get("buttons_avg_pressed") or [])
    triggers = summary.get("trigger_means") or {}

    lx = float(left[0]) if len(left) > 0 else 0.0
    ly = float(left[1]) if len(left) > 1 else 0.0
    # 扳机优先读 trigger_means（旧格式），否则从 buttons_held 推断
    lt = float(triggers.get("LEFT_TRIGGER", 1.0 if "LEFT_TRIGGER" in pressed else 0.0))
    rt = float(triggers.get("RIGHT_TRIGGER", 1.0 if "RIGHT_TRIGGER" in pressed else 0.0))

    stick_mag = (lx * lx + ly * ly) ** 0.5
    attack_score = max(rt, _score_from_pressed(pressed, ATTACK_BUTTONS))
    dodge_score = max(lt, _score_from_pressed(pressed, DODGE_BUTTONS))
    guard_score = lt if lt >= btn_threshold else 0.0

    scores = {
        "ATTACK": attack_score,
        "DODGE": dodge_score,
        "GUARD": guard_score,
        "NAVIGATE": stick_mag * 0.6,
        "WAIT": 0.12,
    }
    primary_intent = max(scores, key=scores.__getitem__)
    confidence = min(float(scores[primary_intent]), 1.0)

    direction = _infer_direction(lx, ly)
    is_change = bool(data.get("is_change"))
    change_info = data.get("change_info") or {}
    change_distance = float(
        change_info.get("distance") or change_info.get("stick_distance") or 0.0
    )

    hint_text = _build_hint_text(pressed, lx, ly, is_change, change_distance)

    horizon = [f"{primary_intent}×{summary.get('chunk_length', 16)}"]

    return PerceptionSignal(
        primary_intent=primary_intent,
        confidence=confidence,
        move_direction=direction,
        move_magnitude=stick_mag,
        horizon_sequence=horizon,
        raw_attack_score=attack_score,
        raw_dodge_score=dodge_score,
        raw_guard_score=guard_score,
        raw_joystick_mag=stick_mag,
        steer=max(-1.0, min(1.0, lx)),
        throttle=1 if rt >= btn_threshold else 0,
        brake=1 if lt >= btn_threshold else 0,
        hint_text=hint_text,
        is_action_change=is_change,
        change_distance=change_distance,
        pressed_buttons=pressed,
    )


def _score_from_pressed(pressed: list[str], names: set[str]) -> float:
    for entry in pressed:
        name = entry.split("(")[0].strip()
        if name in names:
            try:
                val = float(entry.split("(")[1].rstrip(")"))
                return val
            except (IndexError, ValueError):
                return 1.0
    return 0.0


def _infer_direction(lx: float, ly: float) -> str | None:
    mag = (lx * lx + ly * ly) ** 0.5
    if mag < 0.2:
        return None
    if abs(lx) >= abs(ly):
        return "RIGHT" if lx > 0 else "LEFT"
    return "FORWARD" if ly > 0 else "BACK"


def _build_hint_text(
    pressed: list[str],
    lx: float,
    ly: float,
    is_change: bool,
    change_distance: float,
) -> str:
    parts: list[str] = []
    if pressed:
        parts.append("按键 " + ", ".join(pressed))
    if abs(lx) > 0.15 or abs(ly) > 0.15:
        parts.append(f"左摇杆 ({lx:+.2f}, {ly:+.2f})")
    if is_change:
        parts.append(f"检测到动作变化 distance={change_distance:.2f}")
    return "；".join(parts) if parts else "无明显手柄操作"


def format_perception_for_vlm(signal: PerceptionSignal) -> str:
    """供 VLM 使用的高层次动作描述（避免方向盘/油门措辞）。"""
    lines = [
        "快系统动作参考（手柄预测，非驾驶专用语义）:",
        f"- {signal.hint_text or '无摘要'}",
        f"- 高层意图={signal.primary_intent} 置信度={signal.confidence:.0%}",
    ]
    if signal.move_direction:
        lines.append(f"- 摇杆偏向={signal.move_direction}")
    if signal.is_action_change:
        lines.append(f"- 本帧相对近期有动作变化 distance={signal.change_distance:.2f}")
    return "\n".join(lines)
