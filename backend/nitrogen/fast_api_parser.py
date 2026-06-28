"""
将 action_fast_system（远端 FastAPI /predict）JSON 解析为 PerceptionSignal。

支持三种服务端 JSON 格式：
  - raw_v3：j_left / j_right / buttons / button_tokens（模型原始 chunk）
  - schema2：left_stick / buttons_held[{name,value}] / change_info
  - schema1：action_summary.left_stick_mean / buttons_avg_pressed / change_info
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from backend.nitrogen.parser import (
    ATTACK_BUTTONS,
    DODGE_BUTTONS,
    PerceptionSignal,
)
from backend.nitrogen.raw_chunk_adapter import (
    AdapterState,
    is_raw_v3,
    raw_chunk_to_signal,
)

if TYPE_CHECKING:
    pass

_BTN_THRESHOLD = 0.25  # 与 ActionFilter / render_fast BUTTON_CONF_THRESHOLD 对齐


def parse_predict_response(
    data: dict,
    btn_threshold: float = _BTN_THRESHOLD,
    state: AdapterState | None = None,
) -> PerceptionSignal:
    """解析 POST /predict 返回的 JSON（自动识别 schema）。"""
    if is_raw_v3(data):
        return raw_chunk_to_signal(data, btn_threshold, state)

    left, right, pressed, triggers, chunk_len = _extract_motion_and_buttons(data, btn_threshold)

    lx = float(left[0]) if len(left) > 0 else 0.0
    ly = float(left[1]) if len(left) > 1 else 0.0
    lt = float(triggers.get("LEFT_TRIGGER", 0.0))
    rt = float(triggers.get("RIGHT_TRIGGER", 0.0))

    stick_mag = (lx * lx + ly * ly) ** 0.5
    attack_score = max(rt, _score_from_pressed(pressed, ATTACK_BUTTONS))
    dodge_score = max(lt, _score_from_pressed(pressed, DODGE_BUTTONS))
    guard_score = lt if lt >= btn_threshold else 0.0
    navigate_score = stick_mag * 0.6

    # 有实际操作时不让 WAIT 底分（0.12）盖住真实信号
    has_activity = bool(pressed) or stick_mag > 0.15 or attack_score > 0 or dodge_score > 0
    wait_score = 0.0 if has_activity else 0.12

    scores = {
        "ATTACK": attack_score,
        "DODGE": dodge_score,
        "GUARD": guard_score,
        "NAVIGATE": navigate_score,
        "WAIT": wait_score,
    }
    primary_intent = max(scores, key=scores.__getitem__)
    confidence = min(float(scores[primary_intent]), 1.0)

    direction = _infer_direction(lx, ly)
    is_change = bool(data.get("is_change"))
    change_info = data.get("change_info") or {}
    change_distance = _extract_change_distance(change_info)

    hint_text = _build_hint_text(pressed, lx, ly, is_change, change_distance)

    horizon = [f"{primary_intent}×{chunk_len}"]

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


def _is_new_schema(data: dict) -> bool:
    return "left_stick" in data or "buttons_held" in data


def _extract_motion_and_buttons(
    data: dict,
    btn_threshold: float,
) -> tuple[list, list, list[str], dict[str, float], int]:
    if _is_new_schema(data):
        left = list(data.get("left_stick") or [0.0, 0.0])
        right = list(data.get("right_stick") or [0.0, 0.0])
        pressed = _buttons_held_to_pressed(data.get("buttons_held") or [], btn_threshold)
        triggers = _triggers_from_pressed(pressed)
        return left, right, pressed, triggers, 16

    summary = data.get("action_summary") or {}
    left = list(summary.get("left_stick_mean") or [0.0, 0.0])
    right = list(summary.get("right_stick_mean") or [0.0, 0.0])
    pressed = list(summary.get("buttons_avg_pressed") or [])
    triggers = dict(summary.get("trigger_means") or {})
    chunk_len = int(summary.get("chunk_length") or 16)
    return left, right, pressed, triggers, chunk_len


def _buttons_held_to_pressed(buttons_held: list, threshold: float) -> list[str]:
    """新版 buttons_held[{name,value}] → ['SOUTH(0.72)', ...]"""
    out: list[str] = []
    for entry in buttons_held:
        if isinstance(entry, dict):
            name = str(entry.get("name", "")).strip()
            if not name:
                continue
            try:
                val = float(entry.get("value", 1.0))
            except (TypeError, ValueError):
                val = 1.0
            if val >= threshold:
                out.append(f"{name}({val:.2f})")
        elif isinstance(entry, str):
            out.append(entry)
    return out


def _triggers_from_pressed(pressed: list[str]) -> dict[str, float]:
    triggers: dict[str, float] = {"LEFT_TRIGGER": 0.0, "RIGHT_TRIGGER": 0.0}
    for entry in pressed:
        name = entry.split("(")[0].strip()
        if name not in triggers:
            continue
        try:
            triggers[name] = float(entry.split("(")[1].rstrip(")"))
        except (IndexError, ValueError):
            triggers[name] = 1.0
    return triggers


def _extract_change_distance(change_info: dict) -> float:
    if not change_info:
        return 0.0
    if change_info.get("stick_distance") is not None:
        return float(change_info["stick_distance"])
    if change_info.get("distance") is not None:
        return float(change_info["distance"])
    return 0.0


def _score_from_pressed(pressed: list[str], names: set[str]) -> float:
    best = 0.0
    for entry in pressed:
        name = entry.split("(")[0].strip()
        if name not in names:
            continue
        try:
            val = float(entry.split("(")[1].rstrip(")"))
        except (IndexError, ValueError):
            val = 1.0
        best = max(best, val)
    return best


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
