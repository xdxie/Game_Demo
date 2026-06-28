"""
测试 backend/nitrogen/parser.py
覆盖：parse_chunk、意图推断、方向推断、run-length 压缩、chunk offset 补偿
"""

import numpy as np
import pytest

from backend.nitrogen.parser import (
    parse_chunk,
    _run_length_encode,
    _infer_direction,
    CHUNK_OFFSET_START,
    CHUNK_OFFSET_END,
)
from tests.conftest import make_chunk


# ── 基本意图推断 ──────────────────────────────────────────────────────

class TestIntentInference:
    def test_all_zeros_gives_wait(self):
        chunk = make_chunk()
        sig = parse_chunk(chunk)
        assert sig.primary_intent == "WAIT"

    def test_high_attack_buttons_gives_attack(self):
        chunk = make_chunk(attack=0.9)
        sig = parse_chunk(chunk)
        assert sig.primary_intent == "ATTACK"

    def test_high_dodge_buttons_gives_dodge(self):
        chunk = make_chunk(dodge=0.9)
        sig = parse_chunk(chunk)
        assert sig.primary_intent == "DODGE"

    def test_confidence_is_in_range(self):
        for attack in [0.0, 0.5, 0.9]:
            sig = parse_chunk(make_chunk(attack=attack))
            assert 0.0 <= sig.confidence <= 1.0, f"confidence={sig.confidence} out of range"

    def test_confidence_clamped_to_one(self):
        """按钮值超高不应导致 confidence > 1"""
        chunk = make_chunk(attack=2.0)   # 超出 [0,1] 范围
        sig = parse_chunk(chunk)
        assert sig.confidence <= 1.0

    def test_attack_beats_dodge_when_both_present(self):
        """attack 分数明显高于 dodge 时，意图应为 ATTACK"""
        chunk = make_chunk(attack=0.95, dodge=0.3)
        sig = parse_chunk(chunk)
        assert sig.primary_intent == "ATTACK"

    def test_joystick_gives_navigate(self):
        """只有摇杆，无按钮 → NAVIGATE（或 WAIT，取决于幅度阈值）"""
        chunk = make_chunk(jx=0.8, jy=0.0)
        sig = parse_chunk(chunk)
        # 有明显摇杆时，move_magnitude 应该 > 0
        assert sig.move_magnitude > 0


# ── 方向推断 ──────────────────────────────────────────────────────────

class TestDirectionInference:
    def test_left_joystick(self):
        """左摇杆 x < 0 → LEFT"""
        j = np.array([[-0.8, 0.0]] * 10, dtype=np.float32)
        assert _infer_direction(j) == "LEFT"

    def test_right_joystick(self):
        j = np.array([[0.8, 0.0]] * 10, dtype=np.float32)
        assert _infer_direction(j) == "RIGHT"

    def test_forward_joystick(self):
        j = np.array([[0.0, 0.8]] * 10, dtype=np.float32)
        assert _infer_direction(j) == "FORWARD"

    def test_back_joystick(self):
        j = np.array([[0.0, -0.8]] * 10, dtype=np.float32)
        assert _infer_direction(j) == "BACK"

    def test_diagonal_prefers_dominant_axis(self):
        """x 幅度更大时应推断水平方向"""
        j = np.array([[0.8, 0.2]] * 10, dtype=np.float32)
        assert _infer_direction(j) == "RIGHT"

    def test_tiny_joystick_gives_none(self):
        """幅度 < 0.2 不应给出方向"""
        j = np.array([[0.1, 0.05]] * 10, dtype=np.float32)
        assert _infer_direction(j) is None

    def test_parse_chunk_sets_direction(self):
        """parse_chunk 时方向字段应被正确填充"""
        chunk = make_chunk(jx=-0.9)
        sig = parse_chunk(chunk)
        assert sig.move_direction == "LEFT"

    def test_parse_chunk_no_joystick_direction_none(self):
        chunk = make_chunk()
        sig = parse_chunk(chunk)
        assert sig.move_direction is None


# ── chunk offset 补偿 ─────────────────────────────────────────────────

class TestChunkOffsetCompensation:
    def test_offset_constants(self):
        """确认补偿窗口定义正确"""
        assert CHUNK_OFFSET_START == 6
        assert CHUNK_OFFSET_END   == 16

    def test_only_back_half_used_for_intent(self):
        """
        前 6 帧全是 attack，后 10 帧全是 dodge
        → 应推断为 DODGE（后半段权重）
        """
        buttons = np.zeros((16, 21), dtype=np.float32)
        # 前 6 帧：attack（EAST=5）
        buttons[:6, 5]  = 0.95
        buttons[:6, 18] = 0.95
        # 后 10 帧：dodge（LEFT_TRIGGER=9）
        buttons[6:, 9]  = 0.95
        buttons[6:, 7]  = 0.95

        chunk = {
            "j_left":  np.zeros((16, 2), dtype=np.float32),
            "j_right": np.zeros((16, 2), dtype=np.float32),
            "buttons": buttons,
        }
        sig = parse_chunk(chunk)
        assert sig.primary_intent == "DODGE"


# ── horizon sequence（run-length 压缩）────────────────────────────────

class TestRunLengthEncode:
    def test_simple(self):
        assert _run_length_encode(["A", "A", "B"]) == ["A×2", "B×1"]

    def test_all_same(self):
        assert _run_length_encode(["X"] * 5) == ["X×5"]

    def test_empty(self):
        assert _run_length_encode([]) == []

    def test_alternating(self):
        result = _run_length_encode(["A", "B", "A"])
        assert result == ["A×1", "B×1", "A×1"]

    def test_horizon_sequence_in_signal(self):
        """parse_chunk 返回的 horizon_sequence 应为非空列表"""
        sig = parse_chunk(make_chunk(attack=0.8))
        assert isinstance(sig.horizon_sequence, list)
        assert len(sig.horizon_sequence) > 0
        # 格式应含 ×
        assert any("×" in s for s in sig.horizon_sequence)


# ── raw scores 字段 ───────────────────────────────────────────────────

class TestRawScores:
    def test_raw_attack_score_nonzero_on_attack(self):
        sig = parse_chunk(make_chunk(attack=0.8))
        assert sig.raw_attack_score > 0

    def test_raw_dodge_score_nonzero_on_dodge(self):
        sig = parse_chunk(make_chunk(dodge=0.8))
        assert sig.raw_dodge_score > 0

    def test_raw_scores_zero_on_empty_chunk(self):
        sig = parse_chunk(make_chunk())
        assert sig.raw_attack_score == 0.0
        assert sig.raw_dodge_score  == 0.0
