import unittest

from review_coach import ReviewCoach, ReviewRequest


class PlatformerRuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.coach = ReviewCoach()

    def generate(self, query: str) -> dict:
        return self.coach.generate(
            ReviewRequest(
                game_type="platformer",
                game_name="New Super Mario Bros.",
                query=query,
                image_paths=[],
            )
        )

    def assert_rule(self, query: str, event_type: str) -> None:
        result = self.generate(query)
        self.assertTrue(result["should_speak"])
        self.assertEqual(result["event_type"], event_type)
        self.assertGreaterEqual(result["confidence"], 0.7)
        self.assertTrue(result["coaching_text"])

    def test_jump_timing_block(self) -> None:
        self.assert_rule("我是不是跳太早了，那个砖一直没顶到？", "JUMP_TOO_EARLY")

    def test_enemy_reward_priority(self) -> None:
        self.assert_rule("我刚才是不是不该急着拿奖励，下面还有敌人？", "RUSH_TOO_FAST")

    def test_powerup_usage(self) -> None:
        self.assert_rule("那个蘑菇是不是应该回头吃？", "POWERUP_USAGE")

    def test_red_coin_route(self) -> None:
        self.assert_rule("红币那里我路线是不是乱了？", "RUSH_TOO_FAST")

    def test_pit_enemy(self) -> None:
        self.assert_rule("坑边那个小怪我是不是不该硬踩？", "ENEMY_COLLISION")

    def test_coin_greed_collision(self) -> None:
        self.assert_rule("刚才为了金币被撞了，是不是太贪了？", "RUSH_TOO_FAST")

    def test_enemy_rush_wait(self) -> None:
        self.assert_rule("这里我没看清敌人就冲了，应该等一下吗？", "RUSH_TOO_FAST")


if __name__ == "__main__":
    unittest.main()
