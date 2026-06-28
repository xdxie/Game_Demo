"""

快通道模板引擎：GameEvent → 短提示文本（≤8字）。



纯模板，不调用 LLM，延迟 <1ms。

游戏专属词表见 backend/fast/game_vocab.py；在此只做代理。



BUTTON_PRESS 优先级：

  1. 组合键（combo_keys 或 pressed_buttons 命中 combo_to_text 并集）→ 法术/道具名

  2. 单键（button_name 命中 button_to_text）

  3. 空串 → main.py 跳过 TTS



返回空串表示该事件无对应文本，main.py 应跳过 TTS。

"""



from __future__ import annotations



from backend.fast.event import EventType, GameEvent

from backend.fast.game_vocab import get_vocab

from backend.fast.priority import FastPriority



BUTTON_CONF_THRESHOLD = 0.25



_variant_index: dict[tuple, int] = {}





def reset_variant_rotation() -> None:

    """视频 seek 时重置文案轮播索引。"""

    _variant_index.clear()





def _parse_pressed(raw: list | None) -> set[str]:

    """从 ['RIGHT_TRIGGER(0.95)', 'WEST(0.72)'] 提取 conf>=BUTTON_CONF_THRESHOLD 的按键名集合。"""

    if not raw:

        return set()

    out: set[str] = set()

    for entry in raw:

        name = entry.split("(")[0].strip()

        try:

            conf = float(entry.split("(")[1].rstrip(")")) if "(" in entry else 1.0

        except (IndexError, ValueError):

            conf = 1.0

        if conf >= BUTTON_CONF_THRESHOLD and name:

            out.add(name)

    return out





def render_fast(event: GameEvent, game_id: str | None = None) -> str:

    """

    将 GameEvent 渲染为快通道提示文本。



    game_id 由 GameSession.current_game_id 传入；

    为 None 或未注册时自动使用通用词表（GENERAL）。

    只有 trigger_fast=True 的事件才调用此函数。

    """

    vocab = get_vocab(game_id)



    if event.type == EventType.BUTTON_PRESS:

        if event.combo_keys and event.fast_priority == FastPriority.SPELL:

            combo_btns = set(event.combo_keys)

            text = vocab.lookup_combo(combo_btns)

            if text:

                return text

        combo_btns: set[str] | None = None

        if event.combo_keys:

            combo_btns = set(event.combo_keys)

        elif vocab.combo_to_text:

            combo_btns = _parse_pressed(event.perception.pressed_buttons)

        if vocab.combo_to_text and combo_btns:

            combo_text = vocab.lookup_combo(combo_btns)

            if combo_text:

                return combo_text

        if event.button_name:
            btn_variants = vocab.button_variants.get(event.button_name)
            if btn_variants:
                gid = game_id or vocab.game_id
                key = (gid, "btn", event.button_name)
                idx = _variant_index.get(key, 0)
                text = btn_variants[idx % len(btn_variants)]
                _variant_index[key] = idx + 1
                return text
            return vocab.button_to_text.get(event.button_name, "")

        return ""



    variants = vocab.variant_texts.get(event.type)

    if variants:

        key = (game_id or vocab.game_id, event.type)

        idx = _variant_index.get(key, 0)

        text = variants[idx % len(variants)]

        _variant_index[key] = idx + 1

        return text



    pair = vocab.templates.get(event.type)

    if pair is None:

        return vocab.fallback

    has_direction = (

        event.perception.move_direction is not None

        and not vocab.suppress_directional_fast

    )

    return (pair[0] if has_direction else pair[1])(event.perception)


