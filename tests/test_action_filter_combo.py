"""测试 ActionFilter 多候选择优与同帧/跨帧 RT/LT 法术 combo。"""



import pytest



from backend.fast.action_filter import ActionFilter

from backend.fast.event import EventType

from backend.fast.priority import FastPriority

from backend.fast.templates import render_fast

from tests.conftest import make_signal





@pytest.fixture

def af():

    return ActionFilter(

        confidence_threshold=0.4,

        modifier_window_sec=0.5,

        action_change_threshold=0.15,

    )





def p(af, signal, t, interval=0.0):

    return af.process(signal, t, global_min_interval=interval)





class TestPrioritySelection:

    def test_button_beats_direction_on_same_frame(self, af):

        p(af, make_signal("NAVIGATE", direction="LEFT", magnitude=0.8), 0.0)

        ev = p(af, make_signal(

            "NAVIGATE",

            direction="RIGHT",

            magnitude=0.9,

            pressed_buttons=["WEST(0.9)"],

            is_action_change=True,

            change_distance=0.2,

        ), 1.0)

        assert ev is not None

        assert ev.type == EventType.BUTTON_PRESS

        assert ev.fast_priority == FastPriority.BUTTON



    def test_wukong_render_no_direction_on_shift(self, af):

        p(af, make_signal("NAVIGATE", direction="LEFT", magnitude=0.8), 0.0)

        ev = p(af, make_signal("NAVIGATE", direction="RIGHT", magnitude=0.9), 1.0)

        if ev and ev.type == EventType.MOVEMENT_SHIFT:

            text = render_fast(ev, "black_myth_wukong")

            assert "往" not in text

            assert text == "换位置"





class TestCrossFrameCombo:

    def test_rt_then_west_within_window(self, af):

        """RT 按住后 0.4s 内按 WEST → P0 combo。"""

        p(af, make_signal(pressed_buttons=["RIGHT_TRIGGER(0.95)"]), 0.0)

        ev = p(af, make_signal(pressed_buttons=[

            "RIGHT_TRIGGER(0.95)", "WEST(0.85)",

        ]), 0.4)

        assert ev is not None

        assert ev.fast_priority == FastPriority.SPELL

        assert render_fast(ev, "black_myth_wukong") == "给我定！"



    def test_rt_then_west_beyond_window_no_spell(self, af):

        """RT 后超过 0.5s 再按 WEST → 不触发法术 combo。"""

        p(af, make_signal(pressed_buttons=["RIGHT_TRIGGER(0.95)"]), 0.0)

        ev = p(af, make_signal(pressed_buttons=["WEST(0.85)"]), 0.6)

        assert ev is None or ev.fast_priority != FastPriority.SPELL



    def test_same_frame_rt_west_spell(self, af):

        p(af, make_signal(pressed_buttons=[]), 0.0)

        ev = p(af, make_signal(pressed_buttons=[

            "RIGHT_TRIGGER(0.95)", "WEST(0.85)",

        ]), 1.0)

        assert ev is not None

        assert ev.fast_priority == FastPriority.SPELL

        assert render_fast(ev, "black_myth_wukong") == "给我定！"



    def test_lt_dpad_within_window(self, af):

        p(af, make_signal(pressed_buttons=["LEFT_TRIGGER(1.0)"]), 0.0)

        ev = p(af, make_signal(pressed_buttons=[

            "LEFT_TRIGGER(1.0)", "DPAD_UP(0.9)",

        ]), 0.4)

        assert ev is not None

        assert ev.fast_priority == FastPriority.SPELL

        assert render_fast(ev, "black_myth_wukong") == "驱邪散！"



    def test_face_then_rt_within_window(self, af):

        p(af, make_signal(pressed_buttons=["EAST(0.9)"]), 0.0)

        ev = p(af, make_signal(pressed_buttons=["RIGHT_TRIGGER(0.95)"]), 0.4)

        assert ev is not None

        assert ev.fast_priority == FastPriority.SPELL

        assert render_fast(ev, "black_myth_wukong") == "广智救我！"



    def test_face_then_rt_beyond_window_no_spell(self, af):

        p(af, make_signal(pressed_buttons=["NORTH(0.9)"]), 0.0)

        ev = p(af, make_signal(pressed_buttons=["RIGHT_TRIGGER(0.95)"]), 0.6)

        assert ev is None or ev.fast_priority != FastPriority.SPELL



    def test_rt_lt_dual_modifier_spell(self, af):

        p(af, make_signal(pressed_buttons=["RIGHT_TRIGGER(0.95)"]), 0.0)

        ev = p(af, make_signal(pressed_buttons=[

            "RIGHT_TRIGGER(0.95)", "LEFT_TRIGGER(0.9)",

        ]), 0.2)

        assert ev is not None

        assert ev.fast_priority == FastPriority.SPELL

        assert ev.combo_keys == frozenset({"RIGHT_TRIGGER", "LEFT_TRIGGER"})

        assert render_fast(ev, "black_myth_wukong") == "化身！"



    def test_lt_then_rt_within_window(self, af):

        p(af, make_signal(pressed_buttons=["LEFT_TRIGGER(0.9)"]), 0.0)

        ev = p(af, make_signal(pressed_buttons=["RIGHT_TRIGGER(0.95)"]), 0.3)

        assert ev is not None

        assert render_fast(ev, "black_myth_wukong") == "化身！"



    def test_face_then_rt_same_frame_spell(self, af):

        p(af, make_signal(pressed_buttons=["EAST(0.9)"]), 0.0)

        ev = p(af, make_signal(pressed_buttons=[

            "EAST(0.9)", "RIGHT_TRIGGER(0.95)",

        ]), 0.2)

        assert ev is not None

        assert ev.fast_priority == FastPriority.SPELL

        assert render_fast(ev, "black_myth_wukong") == "广智救我！"



    def test_spell_no_cooldown(self, af):

        p(af, make_signal(pressed_buttons=[]), 0.0)

        ev1 = p(af, make_signal(pressed_buttons=[

            "RIGHT_TRIGGER(0.95)", "WEST(0.85)",

        ]), 1.0)

        ev2 = p(af, make_signal(pressed_buttons=[

            "RIGHT_TRIGGER(0.95)", "EAST(0.9)",

        ]), 1.1)

        assert ev1 is not None and ev1.fast_priority == FastPriority.SPELL

        assert ev2 is not None and ev2.fast_priority == FastPriority.SPELL



    @pytest.mark.parametrize("face,expected", [

        ("WEST", "给我定！"),

        ("EAST", "广智救我！"),

        ("NORTH", "聚形散气！"),

        ("SOUTH", "上吧孩儿们！"),

    ])

    def test_rt_face_same_frame(self, af, face, expected):

        p(af, make_signal(pressed_buttons=[f"RIGHT_TRIGGER(0.95)"]), 0.0)

        ev = p(af, make_signal(pressed_buttons=[

            f"RIGHT_TRIGGER(0.95)", f"{face}(0.9)",

        ]), 0.3)

        assert ev is not None

        assert render_fast(ev, "black_myth_wukong") == expected



        af2 = ActionFilter(

            confidence_threshold=0.4,

            modifier_window_sec=0.5,

            action_change_threshold=0.15,

        )

        p(af2, make_signal(pressed_buttons=[f"{face}(0.9)"]), 0.0)

        ev = p(af2, make_signal(pressed_buttons=[

            f"{face}(0.9)", "RIGHT_TRIGGER(0.95)",

        ]), 0.3)

        assert ev is not None

        assert render_fast(ev, "black_myth_wukong") == expected





class TestActionChangeThreshold:

    def test_low_change_distance_ignored(self, af):

        p(af, make_signal("NAVIGATE", direction="LEFT", magnitude=0.8), 0.0)

        ev = p(af, make_signal(

            "NAVIGATE",

            direction="RIGHT",

            magnitude=0.9,

            is_action_change=True,

            change_distance=0.08,

        ), 1.0)

        if ev and ev.type == EventType.MOVEMENT_SHIFT:

            assert ev.fast_priority == FastPriority.DIRECTION

        elif ev is None:

            pass

        else:

            assert ev.type != EventType.MOVEMENT_SHIFT or ev.fast_priority != FastPriority.DIRECTION


