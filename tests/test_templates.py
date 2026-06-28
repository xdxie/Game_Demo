"""Tests for backend/fast/templates.py variant rotation."""

from tests.conftest import make_event, make_signal
from backend.fast.event import EventType
from backend.fast.templates import render_fast, reset_variant_rotation

WUKONG_ID = "black_myth_wukong"


def _sustained_danger_event():
    return make_event(
        EventType.SUSTAINED_DANGER,
        signal=make_signal("DODGE", 0.7, None),
    )


class TestSustainedDangerRotation:
    def setup_method(self):
        reset_variant_rotation()

    def test_wukong_rotates_three_variants(self):
        event = _sustained_danger_event()
        assert render_fast(event, WUKONG_ID) == "拉开距离"
        assert render_fast(event, WUKONG_ID) == "稳住别贪刀"
        assert render_fast(event, WUKONG_ID) == "小心快慢刀"

    def test_wukong_wraps_after_three(self):
        event = _sustained_danger_event()
        for _ in range(3):
            render_fast(event, WUKONG_ID)
        assert render_fast(event, WUKONG_ID) == "拉开距离"

    def test_reset_starts_from_first(self):
        event = _sustained_danger_event()
        render_fast(event, WUKONG_ID)
        render_fast(event, WUKONG_ID)
        reset_variant_rotation()
        assert render_fast(event, WUKONG_ID) == "拉开距离"

    def test_general_uses_template_pair_without_variants(self):
        event = _sustained_danger_event()
        text = render_fast(event)
        assert text == "危险！快拉开距离！"
