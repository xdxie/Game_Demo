"""
ActionSequenceSummarizer — middle layer between NitroGen frame-level action
predictions and ReviewCoach slow/.

Input:  ActionSequenceInput  (or an equivalent dict)
        Each frame carries an `actions` dict mapping semantic labels → confidence:
            {"RIGHT": 0.94, "JUMP": 0.02, "RUN": 0.44, ...}

Output: {
    "action_summary":  str           # ≤80-char Chinese description for slow/ & model
    "action_features": dict          # structured features for rule matching
    "change_info":     dict          # change points, compatible with ReviewRequest.change_info
}

Design constraints
──────────────────
• Pure rule-based, zero LLM calls.  Target latency: <5 ms for a 3–5 s clip.
• Stable JSON output: no floats that vary randomly, no natural-language parsing.
• action_summary uses consistent Chinese vocabulary so PlatformerReviewSkill
  tag keywords still fire when relevant (e.g. "起跳", "跳跃", "方向").
• Backward compatible: the existing `summarize_actions` / `summarize_action_context`
  helpers in this module still work for callers that send NitroGen raw dicts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ── Confidence thresholds ─────────────────────────────────────────────────────

_T_MOVE   = 0.30  # directional action (LEFT / RIGHT / UP / DOWN)
_T_JUMP   = 0.40  # JUMP button
_T_RUN    = 0.35  # RUN / sprint modifier
_T_DOMINANT = 0.55  # fraction of frames needed to call a direction "dominant"


# ── Input dataclasses ─────────────────────────────────────────────────────────

@dataclass
class ActionFrame:
    """Single-frame action snapshot from NitroGen (after semantic normalisation)."""
    frame_idx: int
    timestamp_sec: float
    actions: dict[str, float]   # e.g. {"RIGHT": 0.94, "JUMP": 0.02, "RUN": 0.44}


@dataclass
class ActionSequenceInput:
    """A clip window of ActionFrames, with metadata."""
    frames: list[ActionFrame]
    clip_start_sec: float
    clip_end_sec: float
    fps: float = 10.0
    session_idx: int | None = None


# ── Main class ────────────────────────────────────────────────────────────────

class ActionSequenceSummarizer:
    """
    Compress a per-frame action sequence into structured summaries.

    Typical usage
    ─────────────
    >>> s = ActionSequenceSummarizer()
    >>> result = s.summarize(seq_input)   # ActionSequenceInput or dict
    >>> payload.update(result)            # action_summary / action_features / change_info
    >>> request = ReviewRequest.from_payload(payload)

    NitroGen mario_outputs helper
    ─────────────────────────────
    >>> seq = ActionSequenceSummarizer.from_nitrogen_frames(frame_datas, 12.0, 16.0)
    >>> result = ActionSequenceSummarizer().summarize(seq)
    """

    # ── Public entry point ────────────────────────────────────────────────────

    def summarize(self, inp: ActionSequenceInput | dict) -> dict:
        """
        Summarise the clip and return a dict ready to merge into a ReviewCoach payload.

        Returns
        -------
        {
          "action_summary":  str   ≤80 Chinese chars
          "action_features": dict  (see _extract_features)
          "change_info":     dict  (see _detect_changes)
        }
        """
        if isinstance(inp, dict):
            inp = _parse_input_dict(inp)

        frames = inp.frames
        clip_start = inp.clip_start_sec
        clip_end   = inp.clip_end_sec
        duration   = max(0.0, clip_end - clip_start)

        if not frames:
            return _empty_result(clip_start, clip_end, duration)

        # 1. Per-frame state vectors
        states = [_frame_state(f) for f in frames]

        # 2. Structured features
        features = _extract_features(states, frames, clip_start, duration)

        # 3. Change points
        change_info = _detect_changes(states, frames)

        # 4. Chinese summary ≤80 chars
        action_summary = _generate_summary(features, clip_start)

        return {
            "action_summary":  action_summary,
            "action_features": features,
            "change_info":     change_info,
        }

    # ── NitroGen format bridge ────────────────────────────────────────────────

    @staticmethod
    def from_nitrogen_frames(
        frame_datas: list[dict],
        clip_start_sec: float,
        clip_end_sec: float,
        fps: float = 10.0,
    ) -> ActionSequenceInput:
        """
        Convert mario_outputs / frame_XXXX.json format to ActionSequenceInput.

        Each element of frame_datas should have:
          "action_summary": {"left_stick_mean": [...], "buttons_avg_pressed": [...]}
          "timestamp_sec":  float  (optional, inferred from fps if missing)
          "frame_idx":      int    (optional)
        """
        frames: list[ActionFrame] = []
        for i, data in enumerate(frame_datas):
            t   = _coerce_float(data.get("timestamp_sec")) or (clip_start_sec + i / fps)
            idx = _coerce_int(data.get("frame_idx")) or int(t * fps)
            action_dict = _nitrogen_to_actions(data.get("action_summary") or {})
            frames.append(ActionFrame(frame_idx=idx, timestamp_sec=t, actions=action_dict))
        return ActionSequenceInput(
            frames=frames,
            clip_start_sec=clip_start_sec,
            clip_end_sec=clip_end_sec,
            fps=fps,
        )


# ── Input parsing ─────────────────────────────────────────────────────────────

def _parse_input_dict(d: dict) -> ActionSequenceInput:
    raw_frames = d.get("frames") or []
    frames = []
    for item in raw_frames:
        if isinstance(item, ActionFrame):
            frames.append(item)
            continue
        actions = item.get("actions") or {}
        frames.append(ActionFrame(
            frame_idx=_coerce_int(item.get("frame_idx")) or 0,
            timestamp_sec=_coerce_float(item.get("timestamp_sec")) or 0.0,
            actions={str(k): float(v) for k, v in actions.items()},
        ))
    return ActionSequenceInput(
        frames=frames,
        clip_start_sec=_coerce_float(d.get("clip_start_sec")) or 0.0,
        clip_end_sec=_coerce_float(d.get("clip_end_sec")) or 0.0,
        fps=_coerce_float(d.get("fps")) or 10.0,
        session_idx=_coerce_int(d.get("session_idx")),
    )


# ── Per-frame state ────────────────────────────────────────────────────────────

@dataclass
class _FrameState:
    dir: str | None   # "LEFT" | "RIGHT" | "UP" | "DOWN" | None
    is_jumping: bool
    is_running: bool
    is_idle: bool
    compound: str     # "RIGHT+JUMP", "LEFT", "IDLE", etc. — used in change tracking


def _frame_state(f: ActionFrame) -> _FrameState:
    a = f.actions
    right = a.get("RIGHT", 0.0)
    left  = a.get("LEFT",  0.0)
    up    = a.get("UP",    0.0)
    down  = a.get("DOWN",  0.0)

    # Pick the single strongest direction (no compound direction)
    dirs = [("RIGHT", right), ("LEFT", left), ("UP", up), ("DOWN", down)]
    best_dir, best_conf = max(dirs, key=lambda x: x[1])
    direction = best_dir if best_conf > _T_MOVE else None

    is_jumping = a.get("JUMP", 0.0) > _T_JUMP
    is_running = a.get("RUN",  0.0) > _T_RUN

    parts: list[str] = []
    if direction:
        parts.append(direction)
    if is_jumping:
        parts.append("JUMP")
    compound = "+".join(parts) if parts else "IDLE"
    is_idle  = compound == "IDLE"

    return _FrameState(
        dir=direction,
        is_jumping=is_jumping,
        is_running=is_running,
        is_idle=is_idle,
        compound=compound,
    )


# ── Feature extraction ────────────────────────────────────────────────────────

def _extract_features(
    states: list[_FrameState],
    frames: list[ActionFrame],
    clip_start: float,
    duration: float,
) -> dict:
    n = len(states)

    # Direction counts
    dir_counts: dict[str, int] = {"RIGHT": 0, "LEFT": 0, "UP": 0, "DOWN": 0}
    jump_count_frames = 0
    run_count_frames  = 0
    idle_count_frames = 0

    for st in states:
        if st.dir:
            dir_counts[st.dir] += 1
        if st.is_jumping:
            jump_count_frames += 1
        if st.is_running:
            run_count_frames += 1
        if st.is_idle:
            idle_count_frames += 1

    # Main movement
    max_dir  = max(dir_counts, key=dir_counts.get)  # type: ignore[arg-type]
    max_cnt  = dir_counts[max_dir]
    total_move = sum(dir_counts.values())

    if total_move == 0 or max_cnt / n < 0.10:
        main_movement = "idle"
    elif max_cnt / n >= _T_DOMINANT:
        main_movement = max_dir.lower()
    else:
        significant = sum(1 for v in dir_counts.values() if v / n > 0.20)
        main_movement = max_dir.lower() if significant <= 1 else "mixed"

    # Movement segments
    move_segs = _extract_move_segments(states, frames)

    # Jump segments
    jump_segs = _extract_jump_segments(states, frames)
    jump_count = len(jump_segs)

    # Direction reversal
    direction_reversal = _check_direction_reversal(move_segs)

    # Ratios
    run_ratio  = round(run_count_frames  / n, 3) if n else 0.0
    idle_ratio = round(idle_count_frames / n, 3) if n else 0.0

    # Dominant pattern
    dominant_pattern = _classify_dominant_pattern(
        main_movement, jump_count, direction_reversal,
        idle_ratio, move_segs, jump_segs, clip_start,
    )

    # Risk tags
    risk_tags = _infer_risk_tags(
        main_movement, jump_count, jump_segs, move_segs,
        direction_reversal, idle_ratio, run_ratio, states, frames, clip_start,
    )

    return {
        "duration_sec":       round(duration, 2),
        "main_movement":      main_movement,
        "movement_segments":  [_move_seg_dict(s) for s in move_segs],
        "jump_count":         jump_count,
        "jump_segments":      [_jump_seg_dict(s) for s in jump_segs],
        "run_ratio":          run_ratio,
        "idle_ratio":         idle_ratio,
        "direction_reversal": direction_reversal,
        "dominant_pattern":   dominant_pattern,
        "risk_tags":          risk_tags,
    }


# ── Segment extraction ────────────────────────────────────────────────────────

@dataclass
class _MoveSeg:
    action: str   # LEFT | RIGHT | UP | DOWN
    start_sec: float
    end_sec: float
    total_conf: float
    count: int


@dataclass
class _JumpSeg:
    start_sec: float
    end_sec: float
    total_conf: float
    count: int


def _extract_move_segments(
    states: list[_FrameState], frames: list[ActionFrame]
) -> list[_MoveSeg]:
    segs: list[_MoveSeg] = []
    cur_dir: str | None = None
    seg_start: float = 0.0
    seg_conf: float = 0.0
    seg_count: int = 0

    for st, f in zip(states, frames):
        if st.dir != cur_dir:
            if cur_dir is not None:
                segs.append(_MoveSeg(
                    action=cur_dir,
                    start_sec=seg_start,
                    end_sec=f.timestamp_sec,
                    total_conf=seg_conf,
                    count=seg_count,
                ))
            cur_dir = st.dir
            seg_start = f.timestamp_sec
            seg_conf = 0.0
            seg_count = 0
        if st.dir:
            seg_conf  += f.actions.get(st.dir, 0.0)
            seg_count += 1

    if cur_dir is not None and seg_count > 0:
        segs.append(_MoveSeg(
            action=cur_dir,
            start_sec=seg_start,
            end_sec=frames[-1].timestamp_sec,
            total_conf=seg_conf,
            count=seg_count,
        ))
    return [s for s in segs if s.action is not None]


def _extract_jump_segments(
    states: list[_FrameState], frames: list[ActionFrame]
) -> list[_JumpSeg]:
    segs: list[_JumpSeg] = []
    in_jump = False
    seg_start = 0.0
    seg_conf  = 0.0
    seg_count = 0

    for st, f in zip(states, frames):
        if st.is_jumping and not in_jump:
            in_jump   = True
            seg_start = f.timestamp_sec
            seg_conf  = f.actions.get("JUMP", 0.0)
            seg_count = 1
        elif st.is_jumping and in_jump:
            seg_conf  += f.actions.get("JUMP", 0.0)
            seg_count += 1
        elif not st.is_jumping and in_jump:
            segs.append(_JumpSeg(
                start_sec=seg_start,
                end_sec=f.timestamp_sec,
                total_conf=seg_conf,
                count=seg_count,
            ))
            in_jump   = False
            seg_conf  = 0.0
            seg_count = 0

    if in_jump and seg_count > 0:
        segs.append(_JumpSeg(
            start_sec=seg_start,
            end_sec=frames[-1].timestamp_sec,
            total_conf=seg_conf,
            count=seg_count,
        ))
    return segs


def _check_direction_reversal(segs: list[_MoveSeg]) -> bool:
    dirs = [s.action for s in segs if s.action in ("LEFT", "RIGHT")]
    for i in range(len(dirs) - 1):
        if dirs[i] != dirs[i + 1]:
            return True
    return False


def _move_seg_dict(s: _MoveSeg) -> dict:
    avg_conf = round(s.total_conf / s.count, 3) if s.count else 0.0
    return {
        "action":     s.action,
        "start_sec":  round(s.start_sec, 2),
        "end_sec":    round(s.end_sec, 2),
        "confidence": avg_conf,
    }


def _jump_seg_dict(s: _JumpSeg) -> dict:
    avg_conf = round(s.total_conf / s.count, 3) if s.count else 0.0
    return {
        "start_sec":  round(s.start_sec, 2),
        "end_sec":    round(s.end_sec, 2),
        "confidence": avg_conf,
    }


# ── Dominant pattern classification ───────────────────────────────────────────

def _classify_dominant_pattern(
    main_mv: str,
    jump_count: int,
    direction_reversal: bool,
    idle_ratio: float,
    move_segs: list[_MoveSeg],
    jump_segs: list[_JumpSeg],
    clip_start: float,
) -> str:
    if main_mv == "idle":
        return "idle"

    if main_mv in ("right", "left"):
        sfx = f"_{main_mv}"

        if jump_count == 0:
            return f"run{sfx}"

        # Determine when the first jump happened relative to movement start
        if jump_segs and move_segs:
            move_start = move_segs[0].start_sec
            move_end   = move_segs[-1].end_sec
            span       = max(move_end - move_start, 0.001)
            rel        = (jump_segs[0].start_sec - move_start) / span
            if rel < 0.25:
                return f"jump_then_run{sfx}"
            return f"run{sfx}_then_jump"

        return f"run{sfx}_then_jump"

    if idle_ratio > 0.40:
        return "hesitate_then_move"

    if main_mv == "mixed":
        return "mixed_with_jump" if jump_count > 0 else "mixed"

    return "mixed"


# ── Risk tag inference ────────────────────────────────────────────────────────

def _infer_risk_tags(
    main_mv: str,
    jump_count: int,
    jump_segs: list[_JumpSeg],
    move_segs: list[_MoveSeg],
    direction_reversal: bool,
    idle_ratio: float,
    run_ratio: float,
    states: list[_FrameState],
    frames: list[ActionFrame],
    clip_start: float,
) -> list[str]:
    tags: list[str] = []

    if not frames:
        return tags

    first_t = frames[0].timestamp_sec
    last_t  = frames[-1].timestamp_sec
    clip_dur = max(last_t - first_t, 0.001)

    # Rush: continuous fast movement without pause
    if run_ratio > 0.50 and idle_ratio < 0.05 and jump_count == 0:
        tags.append("rush_possible")

    # Repeated short jumps
    if jump_count >= 3:
        tags.append("repeated_jump")
    elif jump_count == 2 and jump_segs:
        gap = jump_segs[1].start_sec - jump_segs[0].end_sec
        if gap < 1.0:
            tags.append("repeated_jump")

    # Direction correction after jump
    if direction_reversal and jump_count > 0 and jump_segs and move_segs:
        last_jump_end = jump_segs[-1].end_sec
        for seg in move_segs:
            if seg.start_sec >= last_jump_end:
                if (main_mv == "right" and seg.action == "LEFT") or \
                   (main_mv == "left"  and seg.action == "RIGHT"):
                    tags.append("direction_correction_after_jump")
                    break
    elif direction_reversal and jump_count == 0:
        tags.append("reward_greedy_possible")

    # Early / late jump
    if jump_segs:
        first_jump_rel = (jump_segs[0].start_sec - first_t) / clip_dur
        last_jump_rel  = (jump_segs[-1].start_sec - first_t) / clip_dur
        if first_jump_rel < 0.25 and main_mv in ("right", "left"):
            tags.append("early_jump_possible")
        elif last_jump_rel > 0.75 and main_mv in ("right", "left"):
            tags.append("late_jump_possible")

    # Hesitation: significant idle in middle of clip
    if idle_ratio > 0.25 and main_mv != "idle":
        tags.append("hesitation")

    # Edge/gap risk: moving toward something then jumping late
    if main_mv in ("right", "left") and jump_count > 0 and jump_segs:
        last_jump_rel = (jump_segs[-1].start_sec - first_t) / clip_dur
        if last_jump_rel > 0.55:
            tags.append("edge_or_gap_risk_possible")

    # Enemy timing: pause + jump in middle (no sustained movement)
    if jump_segs and idle_ratio > 0.15 and jump_count == 1:
        jump_rel = (jump_segs[0].start_sec - first_t) / clip_dur
        if 0.3 < jump_rel < 0.75:
            tags.append("enemy_timing_risk_possible")

    # Deduplicate while preserving order
    seen: set[str] = set()
    result: list[str] = []
    for tag in tags:
        if tag not in seen:
            seen.add(tag)
            result.append(tag)
    return result


# ── Change detection ──────────────────────────────────────────────────────────

def _detect_changes(states: list[_FrameState], frames: list[ActionFrame]) -> dict:
    if len(states) < 2:
        return {"is_change": False, "change_points": []}

    change_points: list[dict] = []
    prev_compound = states[0].compound

    for st, f in zip(states[1:], frames[1:]):
        if st.compound != prev_compound:
            change_points.append({
                "timestamp_sec": round(f.timestamp_sec, 2),
                "from":          prev_compound,
                "to":            st.compound,
                "reason":        _change_reason(prev_compound, st.compound),
            })
        prev_compound = st.compound

    return {
        "is_change":     len(change_points) > 0,
        "change_points": change_points,
    }


def _change_reason(from_label: str, to_label: str) -> str:
    from_set = set(from_label.split("+")) - {"IDLE"}
    to_set   = set(to_label.split("+"))   - {"IDLE"}
    added    = to_set   - from_set
    removed  = from_set - to_set

    if "JUMP" in added:
        return "jump_started"
    if "JUMP" in removed:
        return "jump_ended"
    if "RIGHT" in added and "LEFT" in removed:
        return "direction_reversed_to_right"
    if "LEFT" in added and "RIGHT" in removed:
        return "direction_reversed_to_left"
    if from_label == "IDLE":
        return "movement_started"
    if to_label == "IDLE":
        return "movement_stopped"
    if "RUN" in added:
        return "run_started"
    if "RUN" in removed:
        return "run_ended"
    return "action_changed"


# ── Chinese summary generator ─────────────────────────────────────────────────

def _generate_summary(features: dict, clip_start: float) -> str:
    main_mv      = features["main_movement"]
    jump_count   = features["jump_count"]
    reversal     = features["direction_reversal"]
    idle_ratio   = features["idle_ratio"]
    run_ratio    = features["run_ratio"]
    risk_tags    = features.get("risk_tags", [])
    jump_segs    = features.get("jump_segments", [])
    duration_sec = features["duration_sec"]

    _DIR_ZH = {"right": "右", "left": "左", "up": "上", "down": "下"}
    parts: list[str] = []

    # ── Main movement ─────────────────────────────────────────────────────────
    if main_mv == "idle":
        parts.append("玩家暂停操作未输入方向")
    elif main_mv in _DIR_ZH:
        d = _DIR_ZH[main_mv]
        if run_ratio > 0.45:
            parts.append(f"玩家持续向{d}加速跑动")
        else:
            parts.append(f"玩家持续向{d}移动")
    elif main_mv == "mixed":
        parts.append("玩家来回反复移动")
    else:
        parts.append("玩家有移动输入")

    # ── Jump info ─────────────────────────────────────────────────────────────
    if jump_count == 1 and jump_segs and duration_sec > 0:
        j_rel = (jump_segs[0]["start_sec"] - clip_start) / duration_sec
        if j_rel < 0.25:
            parts.append("，起手即起跳")
        elif j_rel > 0.70:
            parts.append("，接近目标时起跳")
        else:
            parts.append("，中途起跳一次")
    elif jump_count == 2:
        parts.append("，连续两次起跳")
    elif jump_count >= 3:
        parts.append(f"，短时间内起跳{jump_count}次")

    # ── Direction reversal ────────────────────────────────────────────────────
    if reversal and main_mv in _DIR_ZH:
        if "direction_correction_after_jump" in risk_tags:
            opp = "左" if main_mv == "right" else "右"
            parts.append(f"，落地后向{opp}回拉修正方向")
        else:
            parts.append("，途中短暂反向回拉")

    # ── Hesitation ────────────────────────────────────────────────────────────
    if idle_ratio > 0.25 and main_mv != "idle":
        parts.append("，途中有明显停顿" if idle_ratio > 0.40 else "，途中短暂停顿")

    summary = "".join(parts)
    return summary[:80] if summary else "无明显操作特征"


# ── Fallback for empty input ──────────────────────────────────────────────────

def _empty_result(clip_start: float, clip_end: float, duration: float) -> dict:
    return {
        "action_summary": "无动作数据",
        "action_features": {
            "duration_sec":       round(duration, 2),
            "main_movement":      "idle",
            "movement_segments":  [],
            "jump_count":         0,
            "jump_segments":      [],
            "run_ratio":          0.0,
            "idle_ratio":         1.0,
            "direction_reversal": False,
            "dominant_pattern":   "idle",
            "risk_tags":          [],
        },
        "change_info": {"is_change": False, "change_points": []},
    }


# ── NitroGen format converter ─────────────────────────────────────────────────

def _nitrogen_to_actions(summary: dict) -> dict[str, float]:
    """
    Convert a NitroGen frame_XXXX.json action_summary dict into a normalised
    actions dict: {"RIGHT": 0.94, "JUMP": 0.72, ...}
    """
    actions: dict[str, float] = {
        "LEFT": 0.0, "RIGHT": 0.0, "UP": 0.0, "DOWN": 0.0,
        "JUMP": 0.0, "RUN": 0.0,
    }

    stick = summary.get("left_stick_mean")
    if isinstance(stick, list) and len(stick) >= 2:
        lx, ly = float(stick[0]), float(stick[1])
        if lx > 0:
            actions["RIGHT"] = abs(lx)
        elif lx < 0:
            actions["LEFT"]  = abs(lx)
        if ly > 0.1:
            actions["UP"]    = abs(ly)
        elif ly < -0.1:
            actions["DOWN"]  = abs(ly)

    buttons_raw: list = summary.get("buttons_avg_pressed") or []
    for btn in buttons_raw:
        name = str(btn).split("(")[0].strip()
        conf = 1.0
        if "(" in str(btn) and ")" in str(btn):
            try:
                conf = float(str(btn).split("(")[1].rstrip(")"))
            except ValueError:
                pass
        if name == "SOUTH":
            actions["JUMP"]  = max(actions["JUMP"],  conf)
        elif name in ("EAST", "WEST", "LB", "RB", "NORTH"):
            actions["RUN"]   = max(actions["RUN"],   conf)
        elif name == "DPAD_RIGHT":
            actions["RIGHT"] = max(actions["RIGHT"], conf)
        elif name == "DPAD_LEFT":
            actions["LEFT"]  = max(actions["LEFT"],  conf)
        elif name == "DPAD_UP":
            actions["UP"]    = max(actions["UP"],    conf)
        elif name == "DPAD_DOWN":
            actions["DOWN"]  = max(actions["DOWN"],  conf)

    return actions


# ── Utilities ──────────────────────────────────────────────────────────────────

def _coerce_float(val: Any) -> float | None:
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _coerce_int(val: Any) -> int | None:
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


