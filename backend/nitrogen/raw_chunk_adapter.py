"""
NitroGen schema (3) 原始 chunk → PerceptionSignal。

服务端只返回 j_left / j_right / buttons / button_tokens，无聚合与突变检测。
本模块做 chunk 后半段聚合、按键阈值、客户端帧间 is_action_change。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from backend.nitrogen.parser import (
    ATTACK_BUTTONS,
    CHUNK_OFFSET_START,
    DODGE_BUTTONS,
    PerceptionSignal,
)

STICK_CHANGE_THRESHOLD = 0.3


@dataclass
class AdapterState:
    """跨帧状态：突变检测与 seek 后重置。"""

    prev_pressed_names: set[str] = field(default_factory=set)
    prev_left: tuple[float, float] = (0.0, 0.0)

    def reset(self) -> None:
        self.prev_pressed_names.clear()
        self.prev_left = (0.0, 0.0)


def is_raw_v3(data: dict) -> bool:
    return (
        "j_left" in data
        and "buttons" in data
        and "button_tokens" in data
    )


def raw_chunk_to_signal(
    data: dict,
    btn_threshold: float = 0.25,
    state: AdapterState | None = None,
) -> PerceptionSignal:
    """将 POST /predict 的 schema (3) JSON 转为 PerceptionSignal。"""
    j_left = np.array(data["j_left"], dtype=np.float64)
    buttons = np.array(data["buttons"], dtype=np.float64)
    tokens = list(data["button_tokens"])

    chunk_len = len(buttons)
    start = min(CHUNK_OFFSET_START, max(chunk_len - 1, 0))
    j_eff = j_left[start:chunk_len] if chunk_len > start else j_left
    b_eff = buttons[start:chunk_len] if chunk_len > start else buttons

    if j_eff.size == 0:
        j_eff = j_left
    if b_eff.size == 0:
        b_eff = buttons

    lx = float(j_eff[:, 0].mean()) if j_eff.ndim == 2 and j_eff.shape[1] >= 1 else 0.0
    ly = float(j_eff[:, 1].mean()) if j_eff.ndim == 2 and j_eff.shape[1] >= 2 else 0.0
    stick_mag = float((lx * lx + ly * ly) ** 0.5)

    pressed, btn_means = _aggregate_pressed(tokens, b_eff, btn_threshold)
    lt = float(btn_means.get("LEFT_TRIGGER", 0.0))
    rt = float(btn_means.get("RIGHT_TRIGGER", 0.0))

    attack_score = max(rt, _score_from_means(btn_means, ATTACK_BUTTONS))
    dodge_score = max(
        lt,
        _score_from_means(btn_means, DODGE_BUTTONS),
    )
    guard_score = lt if lt >= btn_threshold else 0.0
    navigate_score = stick_mag * 0.6

    has_activity = (
        bool(pressed)
        or stick_mag > 0.15
        or attack_score > 0
        or dodge_score > 0
    )
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
    is_change, change_distance = _detect_change(lx, ly, pressed, state)

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


def _button_activation(col: np.ndarray, threshold: float) -> float:
    """短脉冲：peak 与 mean 取较大者，避免 2/12 步为 1.0 时被均值稀释。"""
    mean = float(col.mean())
    peak = float(col.max())
    return max(mean, peak)


def _aggregate_pressed(
    tokens: list[str],
    b_eff: np.ndarray,
    threshold: float,
) -> tuple[list[str], dict[str, float]]:
    pressed: list[str] = []
    means: dict[str, float] = {}
    if b_eff.ndim != 2:
        return pressed, means
    n_cols = b_eff.shape[1]
    for k, token in enumerate(tokens):
        if k >= n_cols:
            break
        val = _button_activation(b_eff[:, k], threshold)
        means[token] = val
        if val >= threshold:
            pressed.append(f"{token}({val:.2f})")
    return pressed, means


def _score_from_means(means: dict[str, float], names: set[str]) -> float:
    best = 0.0
    for name in names:
        best = max(best, means.get(name, 0.0))
    return best


def _infer_direction(lx: float, ly: float) -> str | None:
    mag = (lx * lx + ly * ly) ** 0.5
    if mag < 0.2:
        return None
    if abs(lx) >= abs(ly):
        return "RIGHT" if lx > 0 else "LEFT"
    return "FORWARD" if ly > 0 else "BACK"


def _detect_change(
    lx: float,
    ly: float,
    pressed: list[str],
    state: AdapterState | None,
) -> tuple[bool, float]:
    cur_names = {p.split("(")[0].strip() for p in pressed}
    stick_delta = 0.0
    is_change = False

    if state is not None:
        plx, ply = state.prev_left
        stick_delta = float(((lx - plx) ** 2 + (ly - ply) ** 2) ** 0.5)
        is_change = (
            cur_names != state.prev_pressed_names
            or stick_delta > STICK_CHANGE_THRESHOLD
        )
        state.prev_left = (lx, ly)
        state.prev_pressed_names = set(cur_names)

    return is_change, stick_delta


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
