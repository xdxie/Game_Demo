"""
NitroGen 动作向量解析：raw chunk → PerceptionSignal。

NitroGen 输出格式（每次推理返回 action_horizon=16 帧的 chunk）：
  {
    "j_left":  ndarray shape (16, 2),   # 左摇杆 [x, y]，范围 [-1, 1]
    "j_right": ndarray shape (16, 2),   # 右摇杆 [x, y]，范围 [-1, 1]
    "buttons": ndarray shape (16, 21),  # 按钮，float，阈值 0.5 判断是否按下
  }

按钮索引（21个），来自 NitroGen BUTTON_ACTION_TOKENS：
  0:BACK  1:DPAD_DOWN  2:DPAD_LEFT  3:DPAD_RIGHT  4:DPAD_UP
  5:EAST  6:GUIDE  7:LEFT_SHOULDER  8:LEFT_THUMB  9:LEFT_TRIGGER
  10:NORTH  11:RIGHT_BOTTOM  12:RIGHT_LEFT  13:RIGHT_RIGHT
  14:RIGHT_SHOULDER  15:RIGHT_THUMB  16:RIGHT_TRIGGER  17:RIGHT_UP
  18:SOUTH  19:START  20:WEST

推理延迟补偿：
  NitroGen 每 chunk 推理约 200ms，chunk 返回时 chunk[0] 已是过去时。
  使用 chunk[6..15] 的统计（约对应推理完成后的当前时刻）。
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
import numpy as np


# ── 按钮索引常量 ─────────────────────────────────────────────────────
BTN = {
    "BACK": 0, "DPAD_DOWN": 1, "DPAD_LEFT": 2, "DPAD_RIGHT": 3, "DPAD_UP": 4,
    "EAST": 5, "GUIDE": 6, "LEFT_SHOULDER": 7, "LEFT_THUMB": 8,
    "LEFT_TRIGGER": 9, "NORTH": 10, "RIGHT_BOTTOM": 11, "RIGHT_LEFT": 12,
    "RIGHT_RIGHT": 13, "RIGHT_SHOULDER": 14, "RIGHT_THUMB": 15,
    "RIGHT_TRIGGER": 16, "RIGHT_UP": 17, "SOUTH": 18, "START": 19, "WEST": 20,
}

# ── 语义分组（用于意图推断）────────────────────────────────────────
ATTACK_BUTTONS = {"EAST", "SOUTH", "WEST", "NORTH", "RIGHT_TRIGGER"}
DODGE_BUTTONS  = {"LEFT_TRIGGER", "LEFT_SHOULDER", "RIGHT_SHOULDER"}
GUARD_BUTTONS  = {"LEFT_TRIGGER"}
SPECIAL_BUTTONS = {"BACK", "START"}

# ── 推理延迟补偿：使用 chunk 后半段 ────────────────────────────────
CHUNK_OFFSET_START = 6   # 跳过前 6 帧（约 200ms 的推理延迟）
CHUNK_OFFSET_END   = 16  # chunk 总长 16 帧


@dataclass
class PerceptionSignal:
    """从 NitroGen chunk 解析出的感知信号（已去噪、已压缩）"""

    primary_intent: str         # 主导意图：ATTACK / DODGE / GUARD / NAVIGATE / WAIT
    confidence: float           # 意图置信度 [0, 1]
    move_direction: Optional[str]  # 移动方向：LEFT/RIGHT/FORWARD/BACK/None
    move_magnitude: float       # 移动幅度 [0, 1]

    # 未来预测序列（run-length 压缩后），供 VLM 理解"接下来 AI 预测会怎样"
    # 示例：["DODGE×6", "ATTACK×8", "NAVIGATE×2"]
    horizon_sequence: list[str] = field(default_factory=list)

    # 原始平均值（供调试和 2 号调参使用）
    raw_attack_score: float = 0.0
    raw_dodge_score: float  = 0.0
    raw_guard_score: float  = 0.0
    raw_joystick_mag: float = 0.0

    # 简化操控语义（调试面板兼容字段，非驾驶专用）
    steer: float = 0.0      # [-1, 1]，左摇杆 X 分量
    throttle: int = 0       # 0/1，常映射右扳机/确认键强度
    brake: int = 0          # 0/1，常映射左扳机/防御键强度

    # 快系统 HTTP 后处理（action_fast_system）可读摘要
    hint_text: str = ""
    is_action_change: bool = False
    change_distance: float = 0.0
    pressed_buttons: list[str] = field(default_factory=list)


def parse_chunk(chunk: dict, btn_threshold: float = 0.5) -> PerceptionSignal:
    """
    将 NitroGen 原始 chunk 解析为 PerceptionSignal。

    Args:
        chunk: NitroGen 返回的 pred 字典，含 j_left/j_right/buttons
        btn_threshold: 按钮激活阈值（默认 0.5，2号根据实测调整）
    """
    j_left  = np.array(chunk["j_left"])    # (16, 2)
    buttons = np.array(chunk["buttons"])   # (16, 21)

    # ── 使用后半段 chunk 补偿推理延迟 ───────────────────────────────
    j_eff  = j_left[CHUNK_OFFSET_START:CHUNK_OFFSET_END]    # (10, 2)
    b_eff  = buttons[CHUNK_OFFSET_START:CHUNK_OFFSET_END]   # (10, 21)

    # ── 各语义组的平均激活分数 ───────────────────────────────────────
    attack_score = _group_score(b_eff, ATTACK_BUTTONS)
    dodge_score  = _group_score(b_eff, DODGE_BUTTONS)
    guard_score  = _group_score(b_eff, GUARD_BUTTONS)

    joystick_mag = float(np.linalg.norm(j_eff, axis=1).mean())

    # ── 主导意图判断 ─────────────────────────────────────────────────
    scores = {
        "ATTACK":   attack_score,
        "DODGE":    dodge_score,
        "GUARD":    guard_score,
        "NAVIGATE": joystick_mag * 0.5,   # 摇杆幅度归一化为可比较的分数
        "WAIT":     0.1,                  # 基础分，没有其他意图时退化到 WAIT
    }
    primary_intent = max(scores, key=scores.__getitem__)
    confidence = float(scores[primary_intent])
    confidence = min(confidence, 1.0)

    # ── 移动方向 ─────────────────────────────────────────────────────
    move_direction = _infer_direction(j_eff)

    # ── 未来预测序列（全 16 帧，run-length 压缩）────────────────────
    frame_intents = [_frame_intent(buttons[i], b_threshold=btn_threshold)
                     for i in range(16)]
    horizon_sequence = _run_length_encode(frame_intents)

    steer = float(np.clip(j_eff[:, 0].mean(), -1.0, 1.0))
    throttle = 1 if float(b_eff[:, BTN["RIGHT_TRIGGER"]].mean()) >= btn_threshold else 0
    brake = 1 if float(b_eff[:, BTN["LEFT_TRIGGER"]].mean()) >= btn_threshold else 0

    return PerceptionSignal(
        primary_intent=primary_intent,
        confidence=confidence,
        move_direction=move_direction,
        move_magnitude=joystick_mag,
        horizon_sequence=horizon_sequence,
        raw_attack_score=attack_score,
        raw_dodge_score=dodge_score,
        raw_guard_score=guard_score,
        raw_joystick_mag=joystick_mag,
        steer=steer,
        throttle=throttle,
        brake=brake,
    )


# ── 内部工具函数 ─────────────────────────────────────────────────────

def _group_score(b_eff: np.ndarray, btn_names: set[str]) -> float:
    """某语义组在有效 chunk 段内的平均激活分数"""
    indices = [BTN[name] for name in btn_names if name in BTN]
    if not indices:
        return 0.0
    return float(b_eff[:, indices].max(axis=1).mean())


def _infer_direction(j_eff: np.ndarray) -> Optional[str]:
    """根据左摇杆均值推断方向（幅度 < 0.2 视为无明确方向）"""
    mean_x, mean_y = j_eff.mean(axis=0)
    mag = (mean_x ** 2 + mean_y ** 2) ** 0.5
    if mag < 0.2:
        return None
    if abs(mean_x) >= abs(mean_y):
        return "RIGHT" if mean_x > 0 else "LEFT"
    return "FORWARD" if mean_y > 0 else "BACK"


def _frame_intent(btn_row: np.ndarray, b_threshold: float) -> str:
    """单帧的主导意图（简化版，用于生成 horizon 序列）"""
    attack = any(btn_row[BTN[b]] > b_threshold for b in ATTACK_BUTTONS if b in BTN)
    dodge  = any(btn_row[BTN[b]] > b_threshold for b in DODGE_BUTTONS  if b in BTN)
    jmag   = 0.0  # 此处无摇杆数据，用 NAVIGATE 代替摇杆判断（简化）
    if attack:  return "ATTACK"
    if dodge:   return "DODGE"
    return "WAIT"


def _run_length_encode(seq: list[str]) -> list[str]:
    """run-length 压缩，例如 [A,A,A,B,B] → ['A×3', 'B×2']"""
    if not seq:
        return []
    result, cur, count = [], seq[0], 1
    for s in seq[1:]:
        if s == cur:
            count += 1
        else:
            result.append(f"{cur}×{count}")
            cur, count = s, 1
    result.append(f"{cur}×{count}")
    return result
