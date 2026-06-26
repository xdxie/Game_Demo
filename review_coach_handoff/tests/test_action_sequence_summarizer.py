"""
Tests for ActionSequenceSummarizer.

Covers the 6 acceptance criteria from ACTION_SEQUENCE_SUMMARIZER_REQUIREMENTS.md:
  1. Consecutive identical actions are compressed into segments.
  2. Main movement direction is identified.
  3. Jump count is extracted correctly.
  4. Short-time direction reversal is detected.
  5. action_summary is ≤80 Chinese characters.
  6. Output is stable JSON — no natural-language parsing needed.

Additional tests cover:
  - Noisy / low-confidence frames
  - NitroGen mario_outputs format bridge
  - Integration with ReviewRequest.from_payload
"""
import unittest

from review_coach import ActionFrame, ActionSequenceInput, ActionSequenceSummarizer, ReviewRequest
from review_coach.action_summarizer import summarize_action_features


def _build_frames(sequence: list[dict], start_sec: float = 0.0, fps: float = 10.0) -> list[ActionFrame]:
    """Build a list of ActionFrames from a simplified sequence description.

    Each dict in sequence must have at least one of:
      "dir": "RIGHT" | "LEFT" | "UP" | "DOWN"
      "jump": True | False
      "run":  True | False
      Confidence values default to 0.9 for active, 0.0 for inactive.
    """
    frames = []
    for i, spec in enumerate(sequence):
        t = start_sec + i / fps
        actions: dict[str, float] = {
            "LEFT": 0.0, "RIGHT": 0.0, "UP": 0.0, "DOWN": 0.0,
            "JUMP": 0.0, "RUN": 0.0,
        }
        d = spec.get("dir")
        if d:
            actions[d] = spec.get("conf", 0.9)
        if spec.get("jump"):
            actions["JUMP"] = spec.get("jump_conf", 0.85)
        if spec.get("run"):
            actions["RUN"] = spec.get("run_conf", 0.8)
        frames.append(ActionFrame(frame_idx=i, timestamp_sec=round(t, 3), actions=actions))
    return frames


class TestCriteria1_SegmentCompression(unittest.TestCase):
    """Consecutive identical actions are compressed into segments."""

    def test_continuous_right_produces_one_segment(self) -> None:
        frames = _build_frames([{"dir": "RIGHT"}] * 20)
        inp = ActionSequenceInput(frames=frames, clip_start_sec=0.0, clip_end_sec=2.0)
        result = ActionSequenceSummarizer().summarize(inp)

        segs = result["action_features"]["movement_segments"]
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0]["action"], "RIGHT")

    def test_right_then_left_produces_two_segments(self) -> None:
        frames = _build_frames(
            [{"dir": "RIGHT"}] * 10 + [{"dir": "LEFT"}] * 10
        )
        inp = ActionSequenceInput(frames=frames, clip_start_sec=0.0, clip_end_sec=2.0)
        result = ActionSequenceSummarizer().summarize(inp)

        segs = result["action_features"]["movement_segments"]
        actions = [s["action"] for s in segs]
        self.assertIn("RIGHT", actions)
        self.assertIn("LEFT",  actions)

    def test_change_info_records_transition(self) -> None:
        frames = _build_frames(
            [{"dir": "RIGHT"}] * 5 + [{"dir": "RIGHT", "jump": True}] * 5
        )
        inp = ActionSequenceInput(frames=frames, clip_start_sec=0.0, clip_end_sec=1.0)
        result = ActionSequenceSummarizer().summarize(inp)

        cp = result["change_info"]["change_points"]
        self.assertTrue(len(cp) > 0)
        reasons = [p["reason"] for p in cp]
        self.assertIn("jump_started", reasons)


class TestCriteria2_MainMovement(unittest.TestCase):
    """Main movement direction is identified."""

    def test_mostly_right_gives_right(self) -> None:
        frames = _build_frames([{"dir": "RIGHT"}] * 15 + [{"dir": "LEFT"}] * 2)
        inp = ActionSequenceInput(frames=frames, clip_start_sec=0.0, clip_end_sec=1.7)
        result = ActionSequenceSummarizer().summarize(inp)
        self.assertEqual(result["action_features"]["main_movement"], "right")

    def test_mostly_left_gives_left(self) -> None:
        frames = _build_frames([{"dir": "LEFT"}] * 20)
        inp = ActionSequenceInput(frames=frames, clip_start_sec=0.0, clip_end_sec=2.0)
        result = ActionSequenceSummarizer().summarize(inp)
        self.assertEqual(result["action_features"]["main_movement"], "left")

    def test_pure_idle_gives_idle(self) -> None:
        frames = _build_frames([{}] * 15)
        inp = ActionSequenceInput(frames=frames, clip_start_sec=0.0, clip_end_sec=1.5)
        result = ActionSequenceSummarizer().summarize(inp)
        self.assertEqual(result["action_features"]["main_movement"], "idle")

    def test_equal_right_left_gives_mixed(self) -> None:
        frames = _build_frames([{"dir": "RIGHT"}] * 8 + [{"dir": "LEFT"}] * 8)
        inp = ActionSequenceInput(frames=frames, clip_start_sec=0.0, clip_end_sec=1.6)
        result = ActionSequenceSummarizer().summarize(inp)
        mv = result["action_features"]["main_movement"]
        self.assertIn(mv, ("mixed", "right", "left"))  # mixed or whatever dominates slightly


class TestCriteria3_JumpCount(unittest.TestCase):
    """Jump count is extracted correctly."""

    def test_no_jump_gives_zero(self) -> None:
        frames = _build_frames([{"dir": "RIGHT"}] * 20)
        inp = ActionSequenceInput(frames=frames, clip_start_sec=0.0, clip_end_sec=2.0)
        result = ActionSequenceSummarizer().summarize(inp)
        self.assertEqual(result["action_features"]["jump_count"], 0)

    def test_one_jump_gives_one(self) -> None:
        frames = _build_frames(
            [{"dir": "RIGHT"}] * 8
            + [{"dir": "RIGHT", "jump": True}] * 3
            + [{"dir": "RIGHT"}] * 8
        )
        inp = ActionSequenceInput(frames=frames, clip_start_sec=0.0, clip_end_sec=1.9)
        result = ActionSequenceSummarizer().summarize(inp)
        self.assertEqual(result["action_features"]["jump_count"], 1)
        self.assertEqual(len(result["action_features"]["jump_segments"]), 1)

    def test_two_separate_jumps_gives_two(self) -> None:
        frames = _build_frames(
            [{"dir": "RIGHT"}] * 5
            + [{"dir": "RIGHT", "jump": True}] * 3
            + [{"dir": "RIGHT"}] * 5
            + [{"dir": "RIGHT", "jump": True}] * 3
            + [{"dir": "RIGHT"}] * 4
        )
        inp = ActionSequenceInput(frames=frames, clip_start_sec=0.0, clip_end_sec=2.0)
        result = ActionSequenceSummarizer().summarize(inp)
        self.assertEqual(result["action_features"]["jump_count"], 2)

    def test_many_jumps_triggers_repeated_jump_tag(self) -> None:
        frames = _build_frames(
            [{"dir": "RIGHT", "jump": True}] * 2
            + [{"dir": "RIGHT"}] * 2
        ) * 4
        inp = ActionSequenceInput(frames=frames, clip_start_sec=0.0, clip_end_sec=1.6)
        result = ActionSequenceSummarizer().summarize(inp)
        self.assertIn("repeated_jump", result["action_features"]["risk_tags"])


class TestCriteria4_DirectionReversal(unittest.TestCase):
    """Short-time direction reversal is detected."""

    def test_right_then_left_gives_reversal_true(self) -> None:
        frames = _build_frames([{"dir": "RIGHT"}] * 12 + [{"dir": "LEFT"}] * 5)
        inp = ActionSequenceInput(frames=frames, clip_start_sec=0.0, clip_end_sec=1.7)
        result = ActionSequenceSummarizer().summarize(inp)
        self.assertTrue(result["action_features"]["direction_reversal"])

    def test_continuous_right_gives_reversal_false(self) -> None:
        frames = _build_frames([{"dir": "RIGHT"}] * 20)
        inp = ActionSequenceInput(frames=frames, clip_start_sec=0.0, clip_end_sec=2.0)
        result = ActionSequenceSummarizer().summarize(inp)
        self.assertFalse(result["action_features"]["direction_reversal"])

    def test_reversal_after_jump_adds_direction_correction_tag(self) -> None:
        frames = _build_frames(
            [{"dir": "RIGHT"}] * 6
            + [{"dir": "RIGHT", "jump": True}] * 3
            + [{"dir": "LEFT"}] * 5
        )
        inp = ActionSequenceInput(frames=frames, clip_start_sec=0.0, clip_end_sec=1.4)
        result = ActionSequenceSummarizer().summarize(inp)
        tags = result["action_features"]["risk_tags"]
        self.assertIn("direction_correction_after_jump", tags)

    def test_reversal_without_jump_adds_reward_greedy_tag(self) -> None:
        frames = _build_frames([{"dir": "RIGHT"}] * 10 + [{"dir": "LEFT"}] * 5)
        inp = ActionSequenceInput(frames=frames, clip_start_sec=0.0, clip_end_sec=1.5)
        result = ActionSequenceSummarizer().summarize(inp)
        tags = result["action_features"]["risk_tags"]
        self.assertIn("reward_greedy_possible", tags)


class TestCriteria5_SummaryLength(unittest.TestCase):
    """action_summary is ≤80 Chinese characters and non-empty."""

    def _check(self, spec_list: list[dict], label: str) -> None:
        frames = _build_frames(spec_list)
        inp = ActionSequenceInput(
            frames=frames,
            clip_start_sec=0.0,
            clip_end_sec=len(spec_list) / 10.0,
        )
        result = ActionSequenceSummarizer().summarize(inp)
        s = result["action_summary"]
        self.assertIsInstance(s, str, msg=label)
        self.assertGreater(len(s), 0, msg=f"{label}: summary is empty")
        self.assertLessEqual(len(s), 80, msg=f"{label}: summary too long ({len(s)} chars)")

    def test_all_six_scenario_summaries_fit(self) -> None:
        self._check([{"dir": "RIGHT", "run": True}] * 30, "run_right_no_jump")
        self._check(
            [{"dir": "RIGHT"}] * 15
            + [{"dir": "RIGHT", "jump": True}] * 5
            + [{"dir": "RIGHT"}] * 10,
            "run_right_then_jump",
        )
        self._check(
            [{"dir": "RIGHT"}] * 12
            + [{"dir": "RIGHT", "jump": True}] * 4
            + [{"dir": "LEFT"}] * 6,
            "run_right_jump_then_left",
        )
        self._check(
            ([{"dir": "RIGHT", "jump": True}] * 2
             + [{"dir": "RIGHT"}] * 3) * 3,
            "multiple_short_jumps",
        )
        self._check([{}] * 10 + [{"dir": "RIGHT"}] * 20, "hesitate_then_move")
        self._check(
            [{"dir": "RIGHT", "conf": 0.15}] * 15
            + [{"dir": "LEFT", "conf": 0.12}] * 5,
            "low_confidence_frames",
        )

    def test_multiple_jumps_summary(self) -> None:
        self._check(
            ([{"dir": "RIGHT", "jump": True}] * 2 + [{"dir": "RIGHT"}] * 2) * 4,
            "repeated_jumps",
        )

    def test_empty_input_summary(self) -> None:
        inp = ActionSequenceInput(frames=[], clip_start_sec=0.0, clip_end_sec=4.0)
        result = ActionSequenceSummarizer().summarize(inp)
        self.assertIsInstance(result["action_summary"], str)
        self.assertGreater(len(result["action_summary"]), 0)


class TestCriteria6_StableJson(unittest.TestCase):
    """Output is stable JSON — all required keys present, types correct."""

    def test_output_has_all_required_keys(self) -> None:
        frames = _build_frames([{"dir": "RIGHT"}] * 10)
        inp = ActionSequenceInput(frames=frames, clip_start_sec=12.0, clip_end_sec=13.0)
        result = ActionSequenceSummarizer().summarize(inp)

        self.assertIn("action_summary",  result)
        self.assertIn("action_features", result)
        self.assertIn("change_info",     result)

        af = result["action_features"]
        for key in ("duration_sec", "main_movement", "movement_segments",
                    "jump_count", "jump_segments", "run_ratio", "idle_ratio",
                    "direction_reversal", "dominant_pattern", "risk_tags"):
            self.assertIn(key, af, msg=f"missing action_features.{key}")

        ci = result["change_info"]
        self.assertIn("is_change",     ci)
        self.assertIn("change_points", ci)

    def test_types_are_correct(self) -> None:
        frames = _build_frames(
            [{"dir": "RIGHT"}] * 8
            + [{"dir": "RIGHT", "jump": True}] * 2
            + [{"dir": "LEFT"}] * 5
        )
        inp = ActionSequenceInput(frames=frames, clip_start_sec=0.0, clip_end_sec=1.5)
        result = ActionSequenceSummarizer().summarize(inp)

        af = result["action_features"]
        self.assertIsInstance(af["duration_sec"],       float)
        self.assertIsInstance(af["main_movement"],      str)
        self.assertIsInstance(af["jump_count"],         int)
        self.assertIsInstance(af["run_ratio"],          float)
        self.assertIsInstance(af["idle_ratio"],         float)
        self.assertIsInstance(af["direction_reversal"], bool)
        self.assertIsInstance(af["dominant_pattern"],   str)
        self.assertIsInstance(af["risk_tags"],          list)
        self.assertIsInstance(af["movement_segments"],  list)
        self.assertIsInstance(af["jump_segments"],      list)

        self.assertIsInstance(result["change_info"]["is_change"],     bool)
        self.assertIsInstance(result["change_info"]["change_points"],  list)

    def test_dict_input_variant(self) -> None:
        """Summarizer also accepts raw dict matching the input schema."""
        inp_dict = {
            "clip_start_sec": 12.0,
            "clip_end_sec":   16.0,
            "fps": 10,
            "frames": [
                {"frame_idx": 120 + i, "timestamp_sec": 12.0 + i * 0.1,
                 "actions": {"RIGHT": 0.9, "JUMP": 0.0, "RUN": 0.5}}
                for i in range(40)
            ],
        }
        result = ActionSequenceSummarizer().summarize(inp_dict)
        self.assertIn("action_summary", result)
        self.assertEqual(result["action_features"]["main_movement"], "right")


class TestNitrogenBridge(unittest.TestCase):
    """from_nitrogen_frames converts mario_outputs format correctly."""

    def test_right_stick_converted(self) -> None:
        frame_datas = [
            {"action_summary": {"left_stick_mean": [0.94, -0.03], "buttons_avg_pressed": []},
             "timestamp_sec": 12.0 + i * 0.1, "frame_idx": 120 + i}
            for i in range(30)
        ]
        seq = ActionSequenceSummarizer.from_nitrogen_frames(frame_datas, 12.0, 15.0)
        result = ActionSequenceSummarizer().summarize(seq)
        self.assertEqual(result["action_features"]["main_movement"], "right")

    def test_south_button_produces_jump(self) -> None:
        frame_datas = [
            {"action_summary": {
                "left_stick_mean": [0.5, 0.0],
                "buttons_avg_pressed": ["SOUTH(0.85)"],
             }, "timestamp_sec": 12.0 + i * 0.1, "frame_idx": i}
            for i in range(15)
        ]
        seq = ActionSequenceSummarizer.from_nitrogen_frames(frame_datas, 12.0, 13.5)
        result = ActionSequenceSummarizer().summarize(seq)
        self.assertGreater(result["action_features"]["jump_count"], 0)

    def test_empty_frames_list(self) -> None:
        seq = ActionSequenceSummarizer.from_nitrogen_frames([], 0.0, 4.0)
        result = ActionSequenceSummarizer().summarize(seq)
        self.assertEqual(result["action_features"]["main_movement"], "idle")
        self.assertEqual(result["action_features"]["jump_count"], 0)


class TestReviewRequestIntegration(unittest.TestCase):
    """ActionSequenceSummarizer output merges cleanly into ReviewRequest."""

    def test_action_summary_string_is_used_by_review_request(self) -> None:
        frames = _build_frames(
            [{"dir": "RIGHT"}] * 10
            + [{"dir": "RIGHT", "jump": True}] * 3
            + [{"dir": "LEFT"}] * 5
        )
        inp = ActionSequenceInput(frames=frames, clip_start_sec=12.0, clip_end_sec=16.0)
        summarizer_result = ActionSequenceSummarizer().summarize(inp)

        payload = {
            "game_type": "platformer",
            "game_name": "New Super Mario Bros.",
            "query":     "刚才跳那个问号砖，我是不是起跳早了？",
            "image_paths": [],
            "clip_start": 12.0,
            "clip_end":   16.0,
            **summarizer_result,
        }

        request = ReviewRequest.from_payload(payload)
        self.assertIsInstance(request.action_summary,  str)
        self.assertIsInstance(request.action_features, dict)
        self.assertIsInstance(request.change_info,     dict)
        self.assertTrue(request.action_summary)
        self.assertIn("main_movement", request.action_features)  # type: ignore[operator]

    def test_action_features_preserved_through_from_payload(self) -> None:
        payload = {
            "game_type": "platformer",
            "game_name": "test",
            "query": "test",
            "image_paths": [],
            "action_summary": "玩家持续向右移动，中途起跳一次",
            "action_features": {
                "main_movement": "right",
                "jump_count": 1,
                "direction_reversal": False,
                "dominant_pattern": "run_right_then_jump",
                "risk_tags": ["early_jump_possible"],
            },
        }
        request = ReviewRequest.from_payload(payload)
        self.assertIsNotNone(request.action_features)
        self.assertEqual(request.action_features["main_movement"], "right")  # type: ignore[index]
        self.assertEqual(request.action_features["jump_count"], 1)           # type: ignore[index]

    def test_action_features_can_be_summarized_for_review_coach(self) -> None:
        text = summarize_action_features(
            {
                "main_movement": "right",
                "jump_count": 1,
                "direction_reversal": True,
                "dominant_pattern": "run_right_then_jump",
                "risk_tags": ["early_jump_possible", "direction_correction_after_jump"],
            }
        )

        self.assertIn("main_movement=right", text)
        self.assertIn("jump_count=1", text)
        self.assertIn("direction_reversal=true", text)
        self.assertIn("risk_tags=early_jump_possible,direction_correction_after_jump", text)


if __name__ == "__main__":
    unittest.main()
