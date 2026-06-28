"""

动作过滤器：消费 NitroGen 感知信号，检测关键动作和动作突变，输出 GameEvent。



所有阈值均为估算值，需要 2 号在真实视频上运行后调参。

参见 DESIGN.md 9.2 节和 TEAM.md 2号角色说明。

"""



from __future__ import annotations

import logging

import time

from typing import Optional



from backend.fast.event import EventType, GameEvent

from backend.fast.game_vocab import WUKONG_GAME_ID, WukongSpeakPolicy

from backend.fast.priority import FastPriority

from backend.fast.wukong_chunk_scan import (
    extract_timeline,
    filter_hits_to_tail_steps,
    filter_spell_hits_exclude_combo,
    filter_spell_hits_for_new_buttons,
    pick_best_spell_hit_by_confidence,
    scan_wukong_spells,
    scan_wukong_spells_rt_memory,
)

from backend.nitrogen.parser import PerceptionSignal



logger = logging.getLogger(__name__)



BUTTON_CONF_THRESHOLD = 0.25

SPELL_COMBO_DEDUP_SEC = 1.5

FORZA_GAME_ID = "forza_horizon_5"

FORZA_LT_THRESHOLD = 0.15



FACE_KEYS = frozenset({"SOUTH", "EAST", "WEST", "NORTH"})

DPAD_KEYS = frozenset({"DPAD_UP", "DPAD_LEFT", "DPAD_RIGHT", "DPAD_DOWN"})

MODIFIER_KEYS = frozenset({"RIGHT_TRIGGER", "LEFT_TRIGGER"})



_PRIORITY_INTERVALS: dict[FastPriority, float] = {

    FastPriority.SPELL: 0.8,

    FastPriority.BUTTON: 1.5,

    FastPriority.INTENT: 2.0,

    FastPriority.DIRECTION: 4.0,

}





class ActionFilter:

    """三层过滤：结构变化检测 + 显著性门控 + 冷却时间。"""



    def __init__(self,

                 confidence_threshold: float = 0.75,

                 sustained_danger_sec: float = 3.0,

                 cooldowns: Optional[dict] = None,

                 modifier_window_sec: float = 0.5,

                 wukong_rt_modifier_window_sec: float = 1.0,

                 action_change_threshold: float = 0.15):

        self.confidence_threshold = confidence_threshold

        self.sustained_danger_sec = sustained_danger_sec

        self.modifier_window_sec = modifier_window_sec

        self.wukong_rt_modifier_window_sec = wukong_rt_modifier_window_sec

        self.action_change_threshold = action_change_threshold



        self.COOLDOWNS: dict[EventType, float] = {

            EventType.SUDDEN_DODGE:      3.0,

            EventType.ATTACK_WINDOW:     4.0,

            EventType.SUSTAINED_DANGER:  8.0,

            EventType.MOVEMENT_SHIFT:   10.0,

            EventType.PATTERN_COMPLETED: 5.0,

            EventType.BUTTON_PRESS:      1.2,

        }

        if cooldowns:

            for k, v in cooldowns.items():

                try:

                    self.COOLDOWNS[EventType(k)] = v

                except ValueError:

                    pass



        self._last_trigger: dict[EventType | str, float] = {}

        self._prev_signal: Optional[PerceptionSignal] = None



        self._dodge_start: float = 0.0

        self._current_pattern_type: str = "WAIT"

        self._pattern_start: float = 0.0



        self._rt_active_until: float = 0.0

        self._lt_active_until: float = 0.0

        self._face_active_until: dict[str, float] = {}

        self._dpad_active_until: dict[str, float] = {}



        self._last_any_trigger: float = 0.0

        self._last_fast_by_priority: dict[str | FastPriority, float] = {}



        self._signal_count: int = 0

        self._last_diag_wall: float = 0.0

        self._intent_counts: dict[str, int] = {}

        self._filtered_global: int = 0

        self._filtered_cooldown: int = 0

        self._last_spell_combo_at: dict[frozenset[str], float] = {}

        self._game_id: str | None = None



    def process(self,

                signal: PerceptionSignal,

                video_time: float,

                global_min_interval: float = 2.0,

                game_id: str | None = None,

                raw_chunk: dict | None = None,

                replay_clock: bool = False,

                ) -> Optional[GameEvent]:

        wall_now = time.time()

        now = video_time if replay_clock else wall_now

        self._game_id = game_id



        self._signal_count += 1

        intent = signal.primary_intent or "UNKNOWN"

        self._intent_counts[intent] = self._intent_counts.get(intent, 0) + 1

        if self._last_diag_wall == 0.0:

            self._last_diag_wall = wall_now

        elif wall_now - self._last_diag_wall >= 10.0:

            dist = " ".join(f"{k}:{v}" for k, v in sorted(self._intent_counts.items()))

            logger.info(

                "ActionFilter 10s统计: signals=%d intents=[%s] conf=%.2f "

                "filtered(global=%d cooldown=%d) change=%s dir=%s vt=%.1f",

                self._signal_count, dist, signal.confidence,

                self._filtered_global, self._filtered_cooldown,

                signal.is_action_change, signal.move_direction,

                video_time,

            )

            self._intent_counts.clear()

            self._signal_count = 0

            self._filtered_global = 0

            self._filtered_cooldown = 0

            self._last_diag_wall = wall_now



        if game_id == WUKONG_GAME_ID:

            event = self._detect_wukong(signal, video_time, raw_chunk)

        else:

            event = self._detect(signal, video_time)



        if event is None:

            self._prev_signal = signal

            return None



        is_fast_only = event.trigger_fast and not event.trigger_slow

        is_spell = is_fast_only and event.fast_priority == FastPriority.SPELL



        if is_fast_only and not is_spell:

            interval_key, interval = self._fast_interval_key(event, game_id)

            last_pri = self._last_fast_by_priority.get(interval_key, 0.0)

            if last_pri > 0 and now - last_pri < interval:

                self._filtered_global += 1

                logger.info(

                    "ActionFilter 快系统间隔过滤: %s pri=%s (距上次 %.1fs < %.1fs, vt=%.2f)",

                    event.type.value, event.fast_priority.name,

                    now - last_pri, interval, video_time,

                )

                self._prev_signal = signal

                return None

        elif not is_fast_only:

            if (self._last_any_trigger > 0

                    and now - self._last_any_trigger < global_min_interval):

                self._filtered_global += 1

                logger.info(

                    "ActionFilter 全局间隔过滤: %s (距上次 %.1fs < %.1fs, vt=%.2f)",

                    event.type.value,

                    now - self._last_any_trigger,

                    global_min_interval,

                    video_time,

                )

                self._prev_signal = signal

                return None



        if not is_spell:

            cd_key: EventType | str = event.type

            cooldown = self.COOLDOWNS.get(event.type, 3.0)

            if is_fast_only and game_id == WUKONG_GAME_ID and event.type == EventType.BUTTON_PRESS:

                interval_key, interval = self._fast_interval_key(event, game_id)

                if isinstance(interval_key, str) and (

                    interval_key == "wukong_lb"

                    or interval_key.startswith("wukong_t3:")

                ):

                    cd_key = interval_key

                    cooldown = interval

            last = self._last_trigger.get(cd_key, 0.0)

            if last > 0 and now - last < cooldown:

                self._filtered_cooldown += 1

                logger.info(

                    "ActionFilter 冷却过滤: %s (距上次 %.1fs < %.1fs, vt=%.2f)",

                    cd_key if isinstance(cd_key, str) else cd_key.value,

                    now - last, cooldown, video_time,

                )

                self._prev_signal = signal

                return None



        if not is_spell:

            cd_key = event.type

            if is_fast_only and game_id == WUKONG_GAME_ID and event.type == EventType.BUTTON_PRESS:

                interval_key, _ = self._fast_interval_key(event, game_id)

                if isinstance(interval_key, str) and (

                    interval_key == "wukong_lb"

                    or interval_key.startswith("wukong_t3:")

                ):

                    cd_key = interval_key

            self._last_trigger[cd_key] = now

        if is_fast_only and not is_spell:

            interval_key, _ = self._fast_interval_key(event, game_id)

            self._last_fast_by_priority[interval_key] = now

        elif not is_fast_only:

            self._last_any_trigger = now

        self._prev_signal = signal



        logger.info(

            "ActionFilter 触发: %s pri=%s @ vt=%.2f (conf=%.2f, fast=%s slow=%s)",

            event.type.value, event.fast_priority.name, video_time, signal.confidence,

            event.trigger_fast, event.trigger_slow,

        )

        return event



    def reset(self):

        """视频 seek 时调用，重置帧间状态；保留冷却计时器防止 seek 后刷屏。"""

        self._prev_signal = None

        self._dodge_start = 0.0

        self._current_pattern_type = "WAIT"

        self._pattern_start = 0.0

        self._rt_active_until = 0.0

        self._lt_active_until = 0.0

        self._face_active_until.clear()

        self._dpad_active_until.clear()

        self._last_spell_combo_at.clear()



    def _detect(self,

                signal: PerceptionSignal,

                t: float) -> Optional[GameEvent]:

        prev = self._prev_signal

        candidates: list[GameEvent] = []



        self._update_modifier_memory(signal, t)



        forza_brake = self._detect_forza_brake(signal, prev, t)

        if forza_brake is not None:

            candidates.append(forza_brake)



        btn_event = self._detect_button_press(signal, t, prev)

        if btn_event is not None:

            candidates.append(btn_event)



        attack_evt = self._detect_attack_window(signal, t, prev)

        if attack_evt is not None:

            candidates.append(attack_evt)



        danger_evt = self._detect_sustained_danger(signal, t)

        if danger_evt is not None:

            candidates.append(danger_evt)



        pattern_evt = self._detect_pattern_completed(signal, t, prev)

        if pattern_evt is not None:

            candidates.append(pattern_evt)



        dodge_evt = self._detect_sudden_dodge(signal, t, prev)

        if dodge_evt is not None:

            candidates.append(dodge_evt)



        shift_evt = self._detect_movement_shift(signal, t, prev)

        if shift_evt is not None:

            candidates.append(shift_evt)



        change_evt = self._detect_action_change(signal, t)

        if change_evt is not None:

            candidates.append(change_evt)



        if not candidates:

            return None



        types = {e.type for e in candidates}

        if EventType.SUDDEN_DODGE in types and EventType.SUSTAINED_DANGER in types:

            candidates = [e for e in candidates if e.type != EventType.SUSTAINED_DANGER]



        return min(candidates, key=lambda e: (e.fast_priority, e.type.value))



    def _detect_wukong(

        self,

        signal: PerceptionSignal,

        t: float,

        raw_chunk: dict | None,

    ) -> Optional[GameEvent]:

        """黑猴：边沿法术 → 尾步 timeline → RT 记忆补检 → P1/P2 单键 → 慢触发。"""

        prev = self._prev_signal

        self._update_modifier_memory(signal, t)



        deduped_combo: frozenset[str] | None = None

        spell = self._detect_wukong_spell_press(signal, t, prev)

        if spell is not None:

            keys = spell.combo_keys or frozenset()

            emitted = self._emit_wukong_spell_if_fresh(signal, t, keys)

            if emitted is not None:

                return emitted

            deduped_combo = keys



        cur_btns = self._parse_pressed_names_for_game(signal.pressed_buttons)

        prev_btns = (

            self._parse_pressed_names_for_game(prev.pressed_buttons)

            if prev else set()

        )

        new_btns = cur_btns - prev_btns

        rt_active = (

            "RIGHT_TRIGGER" in cur_btns

            or (self._rt_active_until > 0 and t <= self._rt_active_until)

        )

        lt_active = (

            "LEFT_TRIGGER" in cur_btns

            or (self._lt_active_until > 0 and t <= self._lt_active_until)

        )



        if raw_chunk:

            timeline = extract_timeline(raw_chunk)

            if timeline is not None:

                if new_btns:

                    edge_hits = filter_spell_hits_for_new_buttons(

                        scan_wukong_spells(timeline), new_btns,

                    )

                    edge_hits = filter_spell_hits_exclude_combo(

                        edge_hits, deduped_combo,

                    )

                    best = pick_best_spell_hit_by_confidence(edge_hits, timeline)

                    if best is not None:

                        emitted = self._emit_wukong_spell_if_fresh(

                            signal, t, best.combo_keys,

                        )

                        if emitted is not None:

                            return emitted



                face_new = new_btns & FACE_KEYS

                if rt_active and not face_new:

                    mem_hits = filter_spell_hits_exclude_combo(

                        scan_wukong_spells_rt_memory(

                            timeline, rt_active, lt_active,

                        ),

                        deduped_combo,

                    )

                    best = pick_best_spell_hit_by_confidence(mem_hits, timeline)

                    if best is not None:

                        emitted = self._emit_wukong_spell_if_fresh(

                            signal, t, best.combo_keys,

                        )

                        if emitted is not None:

                            return emitted



        if deduped_combo is not None:

            return self._detect_wukong_slow(signal, t, prev)



        return (
            self._detect_wukong_button(signal, t, prev)
            or self._detect_wukong_slow(signal, t, prev)
        )



    def _emit_wukong_spell_if_fresh(

        self,

        signal: PerceptionSignal,

        t: float,

        combo_keys: frozenset[str],

    ) -> Optional[GameEvent]:

        last = self._last_spell_combo_at.get(combo_keys, float("-inf"))

        if t - last < SPELL_COMBO_DEDUP_SEC:

            return None

        self._last_spell_combo_at[combo_keys] = t

        return self._spell_event_from_combo(signal, t, combo_keys)



    def _fast_interval_key(

        self,

        event: GameEvent,

        game_id: str | None,

    ) -> tuple[str | FastPriority, float]:

        if (

            game_id == WUKONG_GAME_ID

            and event.type == EventType.BUTTON_PRESS

            and event.button_name in WukongSpeakPolicy.TIER2_BUTTONS

        ):

            return ("wukong_lb", 2.0)

        if (

            game_id == WUKONG_GAME_ID

            and event.type == EventType.BUTTON_PRESS

            and event.button_name in WukongSpeakPolicy.TIER3_BUTTONS

        ):

            return (f"wukong_t3:{event.button_name}", 0.4)

        pri = event.fast_priority

        interval = _PRIORITY_INTERVALS.get(pri, 2.0)

        return (pri, interval)



    def _detect_wukong_slow(

        self,

        signal: PerceptionSignal,

        t: float,

        prev: Optional[PerceptionSignal],

    ) -> Optional[GameEvent]:

        """黑猴慢触发：反击窗 / 持续危险 / 战斗结束，不恢复走位 fast 噪声。"""

        candidates: list[GameEvent] = []

        attack_evt = self._detect_attack_window(signal, t, prev)

        if attack_evt is not None and attack_evt.trigger_slow:

            candidates.append(attack_evt)

        danger_evt = self._detect_sustained_danger(signal, t)

        if danger_evt is not None and danger_evt.trigger_slow:

            candidates.append(danger_evt)

        pattern_evt = self._detect_pattern_completed(signal, t, prev)

        if pattern_evt is not None and pattern_evt.trigger_slow:

            candidates.append(pattern_evt)

        if not candidates:

            return None

        return min(candidates, key=lambda e: (e.fast_priority, e.type.value))



    def _detect_wukong_spell_press(

        self,

        signal: PerceptionSignal,

        t: float,

        prev: Optional[PerceptionSignal],

    ) -> Optional[GameEvent]:

        """黑猴法术：仅边沿 + 修饰键记忆；跳过 co-presence 与同帧 RT+face 双新按下。"""

        cur_btns = self._parse_pressed_names_for_game(signal.pressed_buttons)

        prev_btns = (

            self._parse_pressed_names_for_game(prev.pressed_buttons)

            if prev else set()

        )

        new_btns = cur_btns - prev_btns

        rt_active = (

            "RIGHT_TRIGGER" in cur_btns

            or (self._rt_active_until > 0 and t <= self._rt_active_until)

        )

        lt_active = (

            "LEFT_TRIGGER" in cur_btns

            or (self._lt_active_until > 0 and t <= self._lt_active_until)

        )

        if not new_btns:

            return None

        edge = self._detect_button_press_edge(

            signal, t, cur_btns, new_btns, rt_active, lt_active,

            defer_rt_face_combo=True,

        )

        if edge is not None and edge.fast_priority == FastPriority.SPELL:

            return edge

        return None



    def _spell_event_from_combo(

        self,

        signal: PerceptionSignal,

        t: float,

        combo_keys: frozenset[str],

    ) -> GameEvent:

        if combo_keys == frozenset({"RIGHT_TRIGGER", "LEFT_TRIGGER"}):

            return self._rt_lt_combo_event(EventType.BUTTON_PRESS, t, signal)

        if "RIGHT_TRIGGER" in combo_keys:

            mod = "RIGHT_TRIGGER"

        else:

            mod = "LEFT_TRIGGER"

        other = next(iter(combo_keys - {mod}))

        return self._spell_combo_event(

            EventType.BUTTON_PRESS, t, signal, mod, other,

        )



    def _detect_wukong_button(

        self,

        signal: PerceptionSignal,

        t: float,

        prev: Optional[PerceptionSignal],

    ) -> Optional[GameEvent]:

        cur_btns = self._parse_pressed_names_for_game(signal.pressed_buttons)

        prev_btns = (

            self._parse_pressed_names_for_game(prev.pressed_buttons)

            if prev else set()

        )

        new_btns = cur_btns - prev_btns

        if not new_btns:

            return None



        tier2 = new_btns & WukongSpeakPolicy.TIER2_BUTTONS

        if tier2:

            btn = next(iter(tier2))

            return self._make_event(

                EventType.BUTTON_PRESS, t, signal,

                fast=True, slow=False,

                button_name=btn,

                fast_priority=FastPriority.BUTTON,

            )



        tier3 = new_btns & WukongSpeakPolicy.TIER3_BUTTONS

        if tier3:

            btn = (

                self._pick_strongest_button(signal.pressed_buttons, tier3)

                or next(iter(tier3))

            )

            return self._make_event(

                EventType.BUTTON_PRESS, t, signal,

                fast=True, slow=False,

                button_name=btn,

                fast_priority=FastPriority.BUTTON,

            )



        return None



    def _pick_best_recent_face(

        self,

        t: float,

        signal: PerceptionSignal,

        cur_btns: set[str],

        faces: set[str],

    ) -> str | None:

        if not faces:

            return None

        held = faces & cur_btns

        if held:

            return (

                self._pick_strongest_button(signal.pressed_buttons, held)

                or max(

                    held,

                    key=lambda k: self._face_active_until.get(k, 0.0),

                )

            )

        return max(

            faces,

            key=lambda k: self._face_active_until.get(k, 0.0),

        )



    def _update_modifier_memory(self, signal: PerceptionSignal, t: float) -> None:

        cur_btns = self._parse_pressed_names_for_game(signal.pressed_buttons)

        rt_window = (

            self.wukong_rt_modifier_window_sec

            if self._game_id == WUKONG_GAME_ID

            else self.modifier_window_sec

        )

        if "RIGHT_TRIGGER" in cur_btns:

            self._rt_active_until = t + rt_window

        if "LEFT_TRIGGER" in cur_btns:

            self._lt_active_until = t + self.modifier_window_sec

        for key in FACE_KEYS:

            if key in cur_btns:

                self._face_active_until[key] = t + self.modifier_window_sec

        for key in DPAD_KEYS:

            if key in cur_btns:

                self._dpad_active_until[key] = t + self.modifier_window_sec



    def _recent_face_keys(self, t: float, cur_btns: set[str]) -> set[str]:

        held = cur_btns & FACE_KEYS

        if held:

            return held

        return {

            key for key in FACE_KEYS

            if key in self._face_active_until and t <= self._face_active_until[key]

        }



    def _recent_dpad_keys(self, t: float, cur_btns: set[str]) -> set[str]:

        held = cur_btns & DPAD_KEYS

        if held:

            return held

        return {

            key for key in DPAD_KEYS

            if key in self._dpad_active_until and t <= self._dpad_active_until[key]

        }



    def _spell_combo_event(

        self,

        evt_type: EventType,

        t: float,

        signal: PerceptionSignal,

        modifier: str,

        face: str,

    ) -> GameEvent:

        return self._make_event(

            evt_type, t, signal,

            fast=True, slow=False,

            button_name=face,

            combo_keys=frozenset({modifier, face}),

            fast_priority=FastPriority.SPELL,

        )



    def _rt_lt_combo_event(

        self,

        evt_type: EventType,

        t: float,

        signal: PerceptionSignal,

    ) -> GameEvent:

        return self._make_event(

            evt_type, t, signal,

            fast=True, slow=False,

            button_name="RIGHT_TRIGGER",

            combo_keys=frozenset({"RIGHT_TRIGGER", "LEFT_TRIGGER"}),

            fast_priority=FastPriority.SPELL,

        )



    def _detect_button_press(

        self,

        signal: PerceptionSignal,

        t: float,

        prev: Optional[PerceptionSignal],

    ) -> Optional[GameEvent]:

        cur_btns = self._parse_pressed_names_for_game(signal.pressed_buttons)

        prev_btns = (
            self._parse_pressed_names_for_game(prev.pressed_buttons)
            if prev else set()
        )

        new_btns = cur_btns - prev_btns

        rt_active = (

            "RIGHT_TRIGGER" in cur_btns

            or (self._rt_active_until > 0 and t <= self._rt_active_until)

        )

        lt_active = (

            "LEFT_TRIGGER" in cur_btns

            or (self._lt_active_until > 0 and t <= self._lt_active_until)

        )

        if new_btns:

            edge = self._detect_button_press_edge(
                signal, t, cur_btns, new_btns, rt_active, lt_active,
            )
            if edge is not None:
                return edge

        if signal.is_action_change:
            return self._try_spell_copresence(signal, t, cur_btns)

        return None



    def _detect_forza_brake(

        self,

        signal: PerceptionSignal,

        prev: Optional[PerceptionSignal],

        t: float,

    ) -> Optional[GameEvent]:

        if self._game_id != FORZA_GAME_ID:

            return None

        prev_brake = prev.brake if prev else 0

        if signal.brake and not prev_brake:

            return self._make_event(

                EventType.BUTTON_PRESS, t, signal,

                fast=True, slow=False,

                button_name="LEFT_TRIGGER",

                fast_priority=FastPriority.BUTTON,

            )

        cur_btns = self._parse_pressed_names_for_game(signal.pressed_buttons)

        prev_btns = (

            self._parse_pressed_names_for_game(prev.pressed_buttons)

            if prev else set()

        )

        if "LEFT_TRIGGER" in (cur_btns - prev_btns):

            return self._make_event(

                EventType.BUTTON_PRESS, t, signal,

                fast=True, slow=False,

                button_name="LEFT_TRIGGER",

                fast_priority=FastPriority.BUTTON,

            )

        return None



    def _detect_button_press_edge(

        self,

        signal: PerceptionSignal,

        t: float,

        cur_btns: set[str],

        new_btns: set[str],

        rt_active: bool,

        lt_active: bool,

        defer_rt_face_combo: bool = False,

    ) -> Optional[GameEvent]:



        # 以下 combo 检测（RT+LT、RT+face、LT+dpad）仅对黑猴有意义，其他游戏跳过
        if self._game_id == WUKONG_GAME_ID:

            # RT + LT 双修饰键（精魄/化身）：同帧或跨帧窗内
            if rt_active and "LEFT_TRIGGER" in new_btns:
                return self._rt_lt_combo_event(EventType.BUTTON_PRESS, t, signal)
            if lt_active and "RIGHT_TRIGGER" in new_btns:
                return self._rt_lt_combo_event(EventType.BUTTON_PRESS, t, signal)

            # RT + 面部键：RT→face 或 face→RT，同帧/0.8s 跨帧

            if rt_active:

                new_face = new_btns & FACE_KEYS

                if new_face:

                    if defer_rt_face_combo and "RIGHT_TRIGGER" in new_btns:

                        return None

                    face = (

                        self._pick_strongest_button(signal.pressed_buttons, new_face)

                        or next(iter(new_face))

                    )

                    return self._spell_combo_event(

                        EventType.BUTTON_PRESS, t, signal, "RIGHT_TRIGGER", face,

                    )

                if "RIGHT_TRIGGER" in new_btns:

                    recent_face = self._recent_face_keys(t, cur_btns)

                    if recent_face:

                        face = self._pick_best_recent_face(

                            t, signal, cur_btns, recent_face,

                        )

                        if face:

                            return self._spell_combo_event(

                                EventType.BUTTON_PRESS, t, signal, "RIGHT_TRIGGER", face,

                            )

            dpad_new = new_btns & DPAD_KEYS

            if lt_active and dpad_new:

                dpad = (

                    self._pick_strongest_button(signal.pressed_buttons, dpad_new)

                    or next(iter(dpad_new))

                )

                return self._spell_combo_event(

                    EventType.BUTTON_PRESS, t, signal, "LEFT_TRIGGER", dpad,

                )

            if "LEFT_TRIGGER" in new_btns:

                recent_dpad = self._recent_dpad_keys(t, cur_btns)

                if recent_dpad:

                    dpad = (

                        self._pick_strongest_button(signal.pressed_buttons, recent_dpad)

                        or next(iter(recent_dpad))

                    )

                    return self._spell_combo_event(

                        EventType.BUTTON_PRESS, t, signal, "LEFT_TRIGGER", dpad,

                    )



        btn = self._pick_strongest_button(signal.pressed_buttons, new_btns)

        if not btn:

            return None

        if btn in MODIFIER_KEYS:

            return None

        return self._make_event(

            EventType.BUTTON_PRESS, t, signal,

            fast=True, slow=False,

            button_name=btn,

            fast_priority=FastPriority.BUTTON,

        )



    def _try_spell_copresence(

        self,

        signal: PerceptionSignal,

        t: float,

        cur_btns: set[str],

    ) -> Optional[GameEvent]:

        """低 fps 漏检边沿时：按键集合变化且 RT+face / RT+LT / LT+dpad 同帧共现。"""

        candidates: list[tuple[frozenset[str], str, str | None]] = []

        if "RIGHT_TRIGGER" in cur_btns and "LEFT_TRIGGER" in cur_btns:

            candidates.append(

                (frozenset({"RIGHT_TRIGGER", "LEFT_TRIGGER"}), "RIGHT_TRIGGER", None),

            )

        if "RIGHT_TRIGGER" in cur_btns:

            faces = cur_btns & FACE_KEYS

            if faces:

                face = (

                    self._pick_strongest_button(signal.pressed_buttons, faces)

                    or next(iter(faces))

                )

                candidates.append(

                    (frozenset({"RIGHT_TRIGGER", face}), "RIGHT_TRIGGER", face),

                )

        if "LEFT_TRIGGER" in cur_btns:

            dpads = cur_btns & DPAD_KEYS

            if dpads:

                dpad = (

                    self._pick_strongest_button(signal.pressed_buttons, dpads)

                    or next(iter(dpads))

                )

                candidates.append(

                    (frozenset({"LEFT_TRIGGER", dpad}), "LEFT_TRIGGER", dpad),

                )

        for combo_keys, modifier, face_or_dpad in candidates:

            last = self._last_spell_combo_at.get(combo_keys, float("-inf"))

            if t - last < SPELL_COMBO_DEDUP_SEC:

                continue

            self._last_spell_combo_at[combo_keys] = t

            if face_or_dpad is None:

                return self._rt_lt_combo_event(

                    EventType.BUTTON_PRESS, t, signal,

                )

            return self._spell_combo_event(

                EventType.BUTTON_PRESS, t, signal, modifier, face_or_dpad,

            )

        return None



    def _detect_sudden_dodge(

        self,

        signal: PerceptionSignal,

        t: float,

        prev: Optional[PerceptionSignal],

    ) -> Optional[GameEvent]:

        if (signal.primary_intent == "DODGE"

                and signal.confidence >= self.confidence_threshold

                and (prev is None or prev.primary_intent != "DODGE")):

            self._dodge_start = t

            return self._make_event(

                EventType.SUDDEN_DODGE, t, signal,

                fast=True, slow=False,

                fast_priority=FastPriority.DIRECTION,

            )

        return None



    def _detect_attack_window(

        self,

        signal: PerceptionSignal,

        t: float,

        prev: Optional[PerceptionSignal],

    ) -> Optional[GameEvent]:

        if (signal.primary_intent == "ATTACK"

                and signal.confidence >= self.confidence_threshold * 0.93

                and prev is not None

                and prev.primary_intent in ("DODGE", "GUARD")):

            return self._make_event(

                EventType.ATTACK_WINDOW, t, signal,

                fast=True, slow=True,

                fast_priority=FastPriority.INTENT,

            )

        return None



    def _detect_sustained_danger(

        self,

        signal: PerceptionSignal,

        t: float,

    ) -> Optional[GameEvent]:

        if signal.primary_intent == "DODGE" and signal.confidence >= self.confidence_threshold * 0.8:

            if self._current_pattern_type == "DODGE":

                duration = t - self._dodge_start

                if duration >= self.sustained_danger_sec:

                    return self._make_event(

                        EventType.SUSTAINED_DANGER, t, signal,

                        fast=True, slow=True,

                        fast_priority=FastPriority.INTENT,

                    )

            else:

                self._dodge_start = t

                self._current_pattern_type = "DODGE"

        else:

            if self._current_pattern_type == "DODGE":

                self._current_pattern_type = signal.primary_intent

        return None



    def _detect_pattern_completed(

        self,

        signal: PerceptionSignal,

        t: float,

        prev: Optional[PerceptionSignal],

    ) -> Optional[GameEvent]:

        non_combat = {"WAIT", "NAVIGATE"}

        was_combat = prev is not None and prev.primary_intent not in non_combat

        now_idle = signal.primary_intent in non_combat

        if was_combat and now_idle:

            self._current_pattern_type = "WAIT"

            self._pattern_start = t

            return self._make_event(

                EventType.PATTERN_COMPLETED, t, signal,

                fast=False, slow=True,

                fast_priority=FastPriority.INTENT,

            )

        return None



    def _detect_movement_shift(

        self,

        signal: PerceptionSignal,

        t: float,

        prev: Optional[PerceptionSignal],

    ) -> Optional[GameEvent]:

        if (prev is not None

                and signal.move_direction is not None

                and prev.move_direction is not None

                and signal.move_direction != prev.move_direction

                and signal.move_magnitude > 0.7):

            return self._make_event(

                EventType.MOVEMENT_SHIFT, t, signal,

                fast=True, slow=False,

                fast_priority=FastPriority.DIRECTION,

            )

        return None



    def _detect_action_change(

        self,

        signal: PerceptionSignal,

        t: float,

    ) -> Optional[GameEvent]:

        if (signal.is_action_change

                and signal.change_distance >= self.action_change_threshold

                and signal.confidence >= self.confidence_threshold * 0.7):

            return self._make_event(

                EventType.MOVEMENT_SHIFT, t, signal,

                fast=True, slow=False,

                fast_priority=FastPriority.DIRECTION,

            )

        return None



    def _parse_pressed_names_for_game(self, raw: list | None) -> set:

        if not raw:

            return set()

        out: set[str] = set()

        for entry in raw:

            name = entry.split("(")[0].strip()

            try:

                conf = float(entry.split("(")[1].rstrip(")")) if "(" in entry else 1.0

            except (IndexError, ValueError):

                conf = 1.0

            threshold = BUTTON_CONF_THRESHOLD

            if self._game_id == FORZA_GAME_ID and name == "LEFT_TRIGGER":

                threshold = FORZA_LT_THRESHOLD

            if conf >= threshold and name:

                out.add(name)

        return out



    @staticmethod

    def _parse_pressed_names(raw: list | None) -> set:

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



    @staticmethod

    def _pick_strongest_button(raw: list | None, candidates: set) -> str | None:

        if not raw:

            return None

        best_name: str | None = None

        best_conf: float = -1.0

        for entry in raw:

            name = entry.split("(")[0].strip()

            if name not in candidates:

                continue

            try:

                conf = float(entry.split("(")[1].rstrip(")")) if "(" in entry else 1.0

            except (IndexError, ValueError):

                conf = 1.0

            if conf > best_conf:

                best_conf = conf

                best_name = name

        return best_name



    @staticmethod

    def _make_event(evt_type: EventType, t: float,

                    signal: PerceptionSignal,

                    fast: bool, slow: bool,

                    button_name: str = "",

                    combo_keys: frozenset[str] | None = None,

                    fast_priority: FastPriority = FastPriority.INTENT) -> GameEvent:

        return GameEvent(

            type=evt_type,

            timestamp=t,

            perception=signal,

            trigger_fast=fast,

            trigger_slow=slow,

            button_name=button_name,

            combo_keys=combo_keys,

            fast_priority=fast_priority,

        )


