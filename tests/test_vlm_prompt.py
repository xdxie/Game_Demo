"""VLM prompt 不含 NitroGen 输入（默认）"""

from backend.nitrogen.controls import signal_from_controls
from backend.fast.event import EventType, GameEvent
from backend.slow.vlm_prompt import build_user_text, system_prompt


def test_build_user_text_default_omits_nitrogen():
    event = GameEvent(
        type=EventType.USER_QUESTION,
        timestamp=5.0,
        perception=signal_from_controls(0.8, 1, 0),
        trigger_fast=False,
        trigger_slow=True,
        user_text="这是什么游戏",
    )
    text = build_user_text(
        event,
        ctx_summary="近15秒动作序列：NAVIGATE×10",
        last_fast_text="无",
        actions_timeline_text="关键动作 steer=+0.75",
        user_question="这是什么游戏",
        include_nitrogen=False,
    )
    assert "steer" not in text
    assert "NAVIGATE" not in text
    assert "关键动作" not in text
    assert "这是什么游戏" in text


from backend.fast.event import EventType, GameEvent
from backend.slow.vlm_prompt import build_user_text, system_prompt
from tests.conftest import make_signal


def test_build_user_text_with_nitrogen():
    event = GameEvent(
        type=EventType.USER_QUESTION,
        timestamp=5.0,
        perception=make_signal("NAVIGATE", 0.9),
        trigger_fast=False,
        trigger_slow=True,
        user_text="test",
    )
    text = build_user_text(
        event,
        ctx_summary="近15秒动作序列：NAVIGATE×3",
        last_fast_text="无",
        actions_timeline_text="timeline here",
        user_question="test",
        include_nitrogen=True,
    )
    assert "快系统动作参考" in text
    assert "timeline here" in text


def test_system_prompt_default_no_nitrogen_mention():
    assert "NitroGen" not in system_prompt(False)
