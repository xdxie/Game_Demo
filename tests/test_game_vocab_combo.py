"""Tests for GameVocab frozenset combo lookup."""

from backend.fast.game_vocab import WUKONG


class TestLookupCombo:
    def test_rt_west_order_independent(self):
        assert WUKONG.lookup_combo({"RIGHT_TRIGGER", "WEST"}) == "给我定！"
        assert WUKONG.lookup_combo({"WEST", "RIGHT_TRIGGER"}) == "给我定！"

    def test_rt_north_order_independent(self):
        assert WUKONG.lookup_combo({"NORTH", "RIGHT_TRIGGER"}) == "聚形散气！"
        assert WUKONG.lookup_combo({"RIGHT_TRIGGER", "NORTH"}) == "聚形散气！"

    def test_all_rt_spells(self):
        assert WUKONG.lookup_combo({"RIGHT_TRIGGER", "EAST"}) == "广智救我！"
        assert WUKONG.lookup_combo({"RIGHT_TRIGGER", "SOUTH"}) == "上吧孩儿们！"

    def test_lt_dpad(self):
        assert WUKONG.lookup_combo({"LEFT_TRIGGER", "DPAD_UP"}) == "驱邪散！"

    def test_single_key_no_match(self):
        assert WUKONG.lookup_combo({"WEST"}) == ""
        assert WUKONG.lookup_combo({"RIGHT_TRIGGER"}) == ""
