import unittest

from review_coach import ReviewCoach, ReviewRequest
from review_coach.action_summarizer import summarize_action_context


class UpstreamPayloadTests(unittest.TestCase):
    def test_from_action_change_payload_ignores_extra_fields(self) -> None:
        payload = {
            "game_type": "platformer",
            "game_name": "New Super Mario Bros.",
            "query": "我刚才是不是不该急着拿奖励，下面还有敌人？",
            "frame_idx": 179,
            "session_idx": 0,
            "auto_reset": False,
            "action_summary": {
                "left_stick_mean": [0.9411, -0.0345],
                "right_stick_mean": [0.0001, 0.0002],
                "left_stick_std": [0.0033, 0.0028],
                "right_stick_std": [0.0021, 0.0018],
                "buttons_avg_pressed": ["SOUTH(0.72)"],
                "trigger_means": {"LEFT_TRIGGER": 0.0, "RIGHT_TRIGGER": 0.0},
                "chunk_length": 18,
            },
            "is_change": True,
            "change_info": {
                "mode": "computed",
                "distance": 1.0683,
                "threshold": 0.7,
                "delta": {"left_stick": [1.234, 0.1329]},
            },
            "source_image": "frame_0179.jpg",
            "client_elapsed_sec": 0.3684,
        }

        request = ReviewRequest.from_payload(payload)
        result = ReviewCoach().generate(request)

        self.assertEqual(request.frame_idx, 179)
        self.assertEqual(request.image_paths, ["frame_0179.jpg"])
        self.assertEqual(result["event_type"], "RUSH_TOO_FAST")
        self.assertTrue(result["coaching_text"])

    def test_summarize_action_change_object(self) -> None:
        summary = summarize_action_context(
            {
                "left_stick_mean": [0.94, -0.03],
                "buttons_avg_pressed": ["SOUTH(0.72)"],
                "trigger_means": {"LEFT_TRIGGER": 0.0, "RIGHT_TRIGGER": 0.4},
            },
            {"distance": 1.06, "threshold": 0.7, "delta": {"left_stick": [1.2, 0.1]}},
        )

        self.assertIn("left_stick=right", summary)
        self.assertIn("buttons=SOUTH(0.72)", summary)
        self.assertIn("change_distance=1.06/0.70", summary)


if __name__ == "__main__":
    unittest.main()
