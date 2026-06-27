"""
快通道模板引擎：GameEvent → 短提示文本（≤8字）。

纯模板，不调用 LLM，延迟 <1ms。
游戏专属词表见 backend/fast/game_vocab.py；在此只做代理。

BUTTON_PRESS 优先级：
  1. 组合键（pressed_buttons 里同时有两键命中 combo_to_text）→ 具体法术/道具名
  2. 单键（button_name 命中 button_to_text）
  3. 空串 → main.py 跳过 TTS

返回空串表示该事件无对应文本，main.py 应跳过 TTS。
"""

from __future__ import annotations
from itertools import combinations

from backend.fast.event import EventType, GameEvent
from backend.fast.game_vocab import get_vocab


def _parse_pressed(raw: list | None) -> set[str]:
    """从 ['RIGHT_TRIGGER(0.95)', 'WEST(0.72)'] 提取 conf>=0.5 的按键名集合。"""
    if not raw:
        return set()
    out: set[str] = set()
    for entry in raw:
        name = entry.split("(")[0].strip()
        try:
            conf = float(entry.split("(")[1].rstrip(")")) if "(" in entry else 1.0
        except (IndexError, ValueError):
            conf = 1.0
        if conf >= 0.5 and name:
            out.add(name)
    return out


def _find_combo(buttons: set[str], combo_map: dict[str, str]) -> str:
    """
    枚举 buttons 中所有 2-键组合，按字母序拼 key 查 combo_map。
    找到第一个命中即返回对应文本；无命中返回空串。
    """
    for a, b in combinations(sorted(buttons), 2):
        key = f"{a}+{b}"
        if key in combo_map:
            return combo_map[key]
    return ""


def render_fast(event: GameEvent, game_id: str | None = None) -> str:
    """
    将 GameEvent 渲染为快通道提示文本。

    game_id 由 GameSession.current_game_id 传入；
    为 None 或未注册时自动使用通用词表（GENERAL）。
    只有 trigger_fast=True 的事件才调用此函数。
    """
    vocab = get_vocab(game_id)

    if event.type == EventType.BUTTON_PRESS:
        # 1. 组合键优先
        cur_btns = _parse_pressed(event.perception.pressed_buttons)
        if vocab.combo_to_text and cur_btns:
            combo_text = _find_combo(cur_btns, vocab.combo_to_text)
            if combo_text:
                return combo_text
        # 2. 单键兜底
        if event.button_name:
            return vocab.button_to_text.get(event.button_name, "")
        return ""

    pair = vocab.templates.get(event.type)
    if pair is None:
        return vocab.fallback
    has_direction = event.perception.move_direction is not None
    return (pair[0] if has_direction else pair[1])(event.perception)
