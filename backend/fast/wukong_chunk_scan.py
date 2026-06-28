"""
黑神话：悟空 — raw chunk 子帧时间线扫描，精筛 RT 法术 combo。

在 mean/peak 聚合之前，对 chunk[6:T] 逐步展开，匹配 RT+face / RT+LT 等同帧或跨步 combo。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from backend.fast.game_vocab import WUKONG, WukongSpeakPolicy
from backend.nitrogen.parser import CHUNK_OFFSET_START
from backend.nitrogen.raw_chunk_adapter import is_raw_v3

BTN_THRESHOLD = 0.25
MODIFIER_THRESHOLD = 0.20
MODIFIER_KEYS = frozenset({"RIGHT_TRIGGER", "LEFT_TRIGGER"})
FACE_KEYS = frozenset({"SOUTH", "EAST", "WEST", "NORTH"})
TAIL_STEP_COUNT = 2
# chunk 内跨步窗（约 0.2–0.3s @10fps 子步）
CROSS_STEP_WINDOW = 3


@dataclass
class ChunkStep:
    index: int
    active: frozenset[str]
    confidences: dict[str, float] = field(default_factory=dict)


@dataclass
class ChunkTimeline:
    steps: list[ChunkStep]
    chunk_len: int


@dataclass
class SpellHit:
    combo_keys: frozenset[str]
    step_index: int
    text: str
    priority: int  # 越小越优先（0=四法术，1=化身，2=LT+dpad）
    face_confidence: float = 0.0


def is_tail_step(step_index: int, timeline: ChunkTimeline) -> bool:
    """是否为 chunk 时间线最后 TAIL_STEP_COUNT 个子帧（按 chunk_len 计）。"""
    if not timeline.steps:
        return False
    cut = max(0, timeline.chunk_len - TAIL_STEP_COUNT)
    return step_index >= cut


def tail_step_indices(timeline: ChunkTimeline) -> set[int]:
    if not timeline.steps:
        return set()
    cut = max(0, timeline.chunk_len - TAIL_STEP_COUNT)
    return {s.index for s in timeline.steps if s.index >= cut}


def extract_timeline(
    data: dict,
    btn_threshold: float = BTN_THRESHOLD,
    modifier_threshold: float = MODIFIER_THRESHOLD,
) -> ChunkTimeline | None:
    """从 schema(3) raw JSON 展开逐步按键时间线。"""
    if not is_raw_v3(data):
        return None
    buttons = np.array(data["buttons"], dtype=np.float64)
    tokens = list(data["button_tokens"])
    chunk_len = len(buttons)
    if buttons.ndim != 2:
        return None
    start = min(CHUNK_OFFSET_START, max(chunk_len - 1, 0))
    steps: list[ChunkStep] = []
    for step in range(start, chunk_len):
        active: set[str] = set()
        conf: dict[str, float] = {}
        for k, token in enumerate(tokens):
            if k >= buttons.shape[1]:
                break
            val = float(buttons[step, k])
            conf[token] = max(conf.get(token, 0.0), val)
            threshold = (
                modifier_threshold if token in MODIFIER_KEYS else btn_threshold
            )
            if val >= threshold:
                active.add(token)
        steps.append(ChunkStep(index=step, active=frozenset(active), confidences=conf))
    return ChunkTimeline(steps=steps, chunk_len=chunk_len)


def _combo_priority(combo: frozenset[str]) -> int:
    if combo == frozenset({"RIGHT_TRIGGER", "LEFT_TRIGGER"}):
        return 1
    if "LEFT_TRIGGER" in combo and combo & FACE_KEYS == frozenset():
        return 2
    if "RIGHT_TRIGGER" in combo and combo & FACE_KEYS:
        return 0
    return 3


def _lookup_combo(combo: frozenset[str]) -> str:
    return WUKONG.lookup_combo(set(combo))


def _face_confidence(step: ChunkStep, combo: frozenset[str]) -> float:
    faces = combo & FACE_KEYS
    if not faces:
        return 0.0
    face = next(iter(faces))
    return step.confidences.get(face, 0.0)


def _make_hit(step: ChunkStep, combo: frozenset[str]) -> SpellHit | None:
    text = _lookup_combo(combo)
    if not text:
        return None
    return SpellHit(
        combo, step.index, text, _combo_priority(combo),
        face_confidence=_face_confidence(step, combo),
    )


def _rt_active_in_window(timeline: ChunkTimeline, step_idx: int, window: int) -> bool:
    for s in timeline.steps:
        if s.index > step_idx:
            break
        if step_idx - s.index > window:
            continue
        if "RIGHT_TRIGGER" in s.active:
            return True
    return False


def _lt_active_in_window(timeline: ChunkTimeline, step_idx: int, window: int) -> bool:
    for s in timeline.steps:
        if s.index > step_idx:
            break
        if step_idx - s.index > window:
            continue
        if "LEFT_TRIGGER" in s.active:
            return True
    return False


def _best_face_at_step(step: ChunkStep, faces: set[str]) -> str | None:
    if not faces:
        return None
    best_name: str | None = None
    best_conf = -1.0
    for name in faces:
        c = step.confidences.get(name, 0.0)
        if c > best_conf:
            best_conf = c
            best_name = name
    return best_name or next(iter(faces))


def scan_wukong_spells_same_step(timeline: ChunkTimeline) -> list[SpellHit]:
    """仅同子帧共现 RT+face / RT+LT / LT+dpad（避免 chunk 内跨步误报）。"""
    hits: list[SpellHit] = []
    seen: set[frozenset[str]] = set()
    for step in timeline.steps:
        active = set(step.active)
        if "RIGHT_TRIGGER" in active and "LEFT_TRIGGER" in active:
            combo = frozenset({"RIGHT_TRIGGER", "LEFT_TRIGGER"})
            hit = _make_hit(step, combo)
            if hit and combo not in seen:
                seen.add(combo)
                hits.append(hit)
        if "RIGHT_TRIGGER" in active:
            faces = active & FACE_KEYS
            if faces:
                face = _best_face_at_step(step, faces)
                if face:
                    combo = frozenset({"RIGHT_TRIGGER", face})
                    hit = _make_hit(step, combo)
                    if hit and combo not in seen:
                        seen.add(combo)
                        hits.append(hit)
        if "LEFT_TRIGGER" in active:
            dpads = active & WukongSpeakPolicy.DPAD_KEYS
            if dpads:
                dpad = _best_face_at_step(step, dpads)
                if dpad:
                    combo = frozenset({"LEFT_TRIGGER", dpad})
                    hit = _make_hit(step, combo)
                    if hit and combo not in seen:
                        seen.add(combo)
                        hits.append(hit)
    hits.sort(key=lambda h: (h.priority, h.step_index))
    return hits


def scan_wukong_spells(timeline: ChunkTimeline) -> list[SpellHit]:
    """
    扫描 chunk 时间线，返回本 chunk 内检测到的法术（已 chunk 内去重）。
    优先 RT+四面部键，其次 RT+LT，最后 LT+dpad 道具 combo。
    """
    hits: list[SpellHit] = []
    seen_combos: set[frozenset[str]] = set()

    for step in timeline.steps:
        active = set(step.active)

        if "RIGHT_TRIGGER" in active and "LEFT_TRIGGER" in active:
            combo = frozenset({"RIGHT_TRIGGER", "LEFT_TRIGGER"})
            hit = _make_hit(step, combo)
            if hit and combo not in seen_combos:
                seen_combos.add(combo)
                hits.append(hit)

        rt_ok = (
            "RIGHT_TRIGGER" in active
            or _rt_active_in_window(timeline, step.index, CROSS_STEP_WINDOW)
        )
        lt_ok = (
            "LEFT_TRIGGER" in active
            or _lt_active_in_window(timeline, step.index, CROSS_STEP_WINDOW)
        )

        if rt_ok:
            faces = active & FACE_KEYS
            if faces:
                face = _best_face_at_step(step, faces)
                if face:
                    combo = frozenset({"RIGHT_TRIGGER", face})
                    hit = _make_hit(step, combo)
                    if hit and combo not in seen_combos:
                        seen_combos.add(combo)
                        hits.append(hit)

        if rt_ok and "LEFT_TRIGGER" in active:
            combo = frozenset({"RIGHT_TRIGGER", "LEFT_TRIGGER"})
            hit = _make_hit(step, combo)
            if hit and combo not in seen_combos:
                seen_combos.add(combo)
                hits.append(hit)
        if lt_ok and "RIGHT_TRIGGER" in active:
            combo = frozenset({"RIGHT_TRIGGER", "LEFT_TRIGGER"})
            hit = _make_hit(step, combo)
            if hit and combo not in seen_combos:
                seen_combos.add(combo)
                hits.append(hit)

        if lt_ok:
            dpads = active & WukongSpeakPolicy.DPAD_KEYS
            if dpads:
                dpad = _best_face_at_step(step, dpads)
                if dpad:
                    combo = frozenset({"LEFT_TRIGGER", dpad})
                    hit = _make_hit(step, combo)
                    if hit and combo not in seen_combos:
                        seen_combos.add(combo)
                        hits.append(hit)

    hits.sort(key=lambda h: (h.priority, h.step_index))
    return hits


def scan_wukong_spells_rt_memory(
    timeline: ChunkTimeline,
    rt_active: bool,
    lt_active: bool,
    btn_threshold: float = BTN_THRESHOLD,
) -> list[SpellHit]:
    """
    跨帧 RT/LT 记忆补检：在 chunk 尾步找 face/dpad 峰值，不要求聚合 new_btns。
    用于 8fps 漏边沿或 RT 与 face 分帧场景。
    """
    if not timeline.steps:
        return []
    hits: list[SpellHit] = []
    seen: set[frozenset[str]] = set()
    tail_steps = [s for s in timeline.steps if is_tail_step(s.index, timeline)]

    for step in tail_steps:
        active = set(step.active)
        faces = {
            name for name in FACE_KEYS
            if step.confidences.get(name, 0.0) >= btn_threshold
        }

        if rt_active and lt_active and (
            "RIGHT_TRIGGER" in active or "LEFT_TRIGGER" in active
        ):
            combo = frozenset({"RIGHT_TRIGGER", "LEFT_TRIGGER"})
            hit = _make_hit(step, combo)
            if hit and combo not in seen:
                seen.add(combo)
                hits.append(hit)

        if rt_active and faces:
            face = _best_face_at_step(step, faces)
            if face:
                combo = frozenset({"RIGHT_TRIGGER", face})
                hit = _make_hit(step, combo)
                if hit and combo not in seen:
                    seen.add(combo)
                    hits.append(hit)

        if lt_active:
            dpads = {
                name for name in WukongSpeakPolicy.DPAD_KEYS
                if step.confidences.get(name, 0.0) >= btn_threshold
            }
            if dpads:
                dpad = _best_face_at_step(step, dpads)
                if dpad:
                    combo = frozenset({"LEFT_TRIGGER", dpad})
                    hit = _make_hit(step, combo)
                    if hit and combo not in seen:
                        seen.add(combo)
                        hits.append(hit)

    hits.sort(key=lambda h: (h.priority, -h.face_confidence, h.step_index))
    return hits


def pick_best_spell_hit(hits: list[SpellHit]) -> SpellHit | None:
    """取优先级最高、步序最晚的一条。"""
    if not hits:
        return None
    pri = min(h.priority for h in hits)
    candidates = [h for h in hits if h.priority == pri]
    return max(candidates, key=lambda h: h.step_index)


def pick_best_spell_hit_by_confidence(
    hits: list[SpellHit],
    timeline: ChunkTimeline | None = None,
) -> SpellHit | None:
    """同 priority 下取 face 置信度最高者，其次步序最晚。"""
    if not hits:
        return None
    pri = min(h.priority for h in hits)
    candidates = [h for h in hits if h.priority == pri]

    def _score(h: SpellHit) -> tuple[float, int]:
        conf = h.face_confidence
        if conf <= 0.0 and timeline is not None:
            for s in timeline.steps:
                if s.index == h.step_index:
                    conf = _face_confidence(s, h.combo_keys)
                    break
        return (conf, h.step_index)

    return max(candidates, key=_score)


def filter_hits_to_tail_steps(
    hits: list[SpellHit],
    timeline: ChunkTimeline,
) -> list[SpellHit]:
    """仅保留 chunk 尾步命中，降低前半段 RT+face 噪声。"""
    tail = tail_step_indices(timeline)
    if not tail:
        return hits
    return [h for h in hits if h.step_index in tail]


def filter_spell_hits_for_new_buttons(
    hits: list[SpellHit],
    new_btns: set[str],
) -> list[SpellHit]:
    """
    仅保留与聚合信号边沿一致的 timeline 命中：
    face/dpad 须在 new_btns；RT+LT 至少一侧为新按下。
    同帧 RT+face 双新按下（聚合伪共现）跳过。
    """
    if not new_btns:
        return []
    rt_lt = frozenset({"RIGHT_TRIGGER", "LEFT_TRIGGER"})
    out: list[SpellHit] = []
    for hit in hits:
        combo = hit.combo_keys
        if combo == rt_lt:
            if new_btns & rt_lt:
                out.append(hit)
            continue
        if "RIGHT_TRIGGER" in combo:
            faces = combo & FACE_KEYS
            if not faces:
                continue
            face = next(iter(faces))
            if face not in new_btns:
                continue
            if "RIGHT_TRIGGER" in new_btns and face in new_btns:
                continue
            out.append(hit)
            continue
        if "LEFT_TRIGGER" in combo:
            dpads = combo & WukongSpeakPolicy.DPAD_KEYS
            if not dpads:
                continue
            dpad = next(iter(dpads))
            if dpad not in new_btns:
                continue
            if "LEFT_TRIGGER" in new_btns and dpad in new_btns:
                continue
            out.append(hit)
    return out


def filter_spell_hits_exclude_combo(
    hits: list[SpellHit],
    excluded: frozenset[str] | None,
) -> list[SpellHit]:
    if not excluded:
        return hits
    return [h for h in hits if h.combo_keys != excluded]
