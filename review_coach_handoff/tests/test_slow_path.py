import unittest

from review_coach import SlowPath
from review_coach.slow import ContextBuffer
from review_coach.slow.trigger import SlowPriority, should_trigger_slow


class FakeCoach:
    def generate(self, request):
        return {
            "should_speak": True,
            "game_type": request.game_type,
            "event_type": request.trigger_reason or "GENERAL_REVIEW",
            "problem": "fake",
            "coaching_text": "这段先稳住节奏，等安全窗口出现后再继续推进。",
            "confidence": 0.8,
        }


class SlowPathTests(unittest.TestCase):
    def test_context_buffer_summarizes_recent_signals_and_events(self) -> None:
        context = ContextBuffer(window_sec=15)
        context.push_signal(1.0, {"primary_intent": "NAVIGATE", "confidence": 0.8})
        context.push_signal(2.0, {"primary_intent": "NAVIGATE", "confidence": 0.7})
        context.push_signal(3.0, {"primary_intent": "DODGE", "confidence": 0.9, "horizon": ["DODGE", "ATTACK"]})
        context.push_event(3.0, {"type": "attack_window"})

        summary = context.summarize()

        self.assertIn("NAVIGATEx2", summary)
        self.assertIn("DODGEx1", summary)
        self.assertIn("attack_window", summary)

    def test_should_trigger_slow(self) -> None:
        self.assertTrue(should_trigger_slow({"type": "user_question"}))
        self.assertTrue(should_trigger_slow({"type": "pattern_completed"}))
        self.assertTrue(should_trigger_slow({"type": "attack_window", "trigger_slow": True}))
        self.assertFalse(should_trigger_slow({"type": "sudden_dodge", "trigger_slow": False}))

    def test_user_question_has_user_answer_priority(self) -> None:
        slow_path = SlowPath()
        result = slow_path.handle(
            {"type": "user_question"},
            {
                "game_type": "platformer",
                "game_name": "New Super Mario Bros.",
                "image_paths": [],
            },
            user_question="我是不是跳太早了，那个砖一直没顶到？",
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.priority, SlowPriority.USER_ANSWER)
        self.assertEqual(result.channel, "user_answer")
        self.assertTrue(result.interrupt_tts)
        self.assertEqual(result.clear_pending_channels, ("slow", "slow_summary"))
        self.assertEqual(result.expire_sec, 30.0)
        self.assertIn("起跳", result.text)

    def test_pattern_completed_is_summary_priority(self) -> None:
        slow_path = SlowPath(coach=FakeCoach())
        slow_path.observe_signal(1.0, {"primary_intent": "NAVIGATE"})
        slow_path.observe_signal(2.0, {"primary_intent": "DODGE"})
        result = slow_path.handle(
            {"type": "pattern_completed"},
            {
                "game_type": "platformer",
                "game_name": "New Super Mario Bros.",
                "image_paths": [],
            },
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.priority, SlowPriority.SLOW_SUMMARY)
        self.assertEqual(result.channel, "slow_summary")
        self.assertFalse(result.interrupt_tts)
        self.assertEqual(result.clear_pending_channels, ())
        self.assertEqual(result.expire_sec, 15.0)
        self.assertTrue(result.text)

    def test_trigger_slow_event_is_advice_priority(self) -> None:
        slow_path = SlowPath(coach=FakeCoach())
        result = slow_path.handle(
            {"type": "attack_window", "trigger_slow": True},
            {
                "game_type": "platformer",
                "game_name": "New Super Mario Bros.",
                "image_paths": [],
            },
            last_fast_text="有机会，打！",
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.priority, SlowPriority.SLOW_ADVICE)
        self.assertEqual(result.channel, "slow")
        self.assertFalse(result.interrupt_tts)
        self.assertEqual(result.clear_pending_channels, ())
        self.assertEqual(result.expire_sec, 8.0)

    def test_non_slow_event_returns_none(self) -> None:
        slow_path = SlowPath()
        result = slow_path.handle(
            {"type": "sudden_dodge", "trigger_slow": False},
            {"game_type": "platformer", "game_name": "New Super Mario Bros."},
        )

        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
