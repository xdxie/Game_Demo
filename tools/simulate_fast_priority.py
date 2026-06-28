"""
离线模拟快系统 P0/P1/P3 触发统计。

用法：
  python tools/simulate_fast_priority.py
  python tools/simulate_fast_priority.py path/to/wukong_actions.jsonl

无 JSONL 时使用内置 1415 帧合成序列（按键边沿 + 方向突变 + RT combo）。
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from backend.fast.action_filter import ActionFilter
from backend.fast.output_gate import FastOutputGate, FastGateConfig
from backend.fast.priority import FastPriority
from backend.fast.templates import render_fast
from backend.nitrogen.fast_api_parser import parse_predict_response
from backend.nitrogen.parser import PerceptionSignal


GAME_ID = "black_myth_wukong"
FRAME_COUNT = 1415


def _synthetic_frames(n: int) -> list[PerceptionSignal]:
    """合成序列：模拟黑猴常见帧型（方向噪声 + 稀疏按键 + RT combo）。"""
    frames: list[PerceptionSignal] = []
    dirs = ["LEFT", "RIGHT", "FORWARD", "BACK", None]
    rt_held = False
    for i in range(n):
        direction = dirs[i % len(dirs)]
        mag = 0.55 + (i % 5) * 0.08
        pressed: list[str] = []
        is_change = (i % 3 == 0)
        change_dist = 0.2 if is_change else 0.02

        if i % 47 == 10:
            rt_held = True
            pressed.append("RIGHT_TRIGGER(0.95)")
        if i % 47 == 11 and rt_held:
            pressed.extend(["RIGHT_TRIGGER(0.95)", "WEST(0.85)"])
            rt_held = False
        elif i % 23 == 7:
            pressed.append("WEST(0.88)")
        elif i % 31 == 5:
            pressed.append("EAST(0.82)")
        elif i % 19 == 3:
            pressed.append("RIGHT_SHOULDER(0.9)")

        frames.append(PerceptionSignal(
            primary_intent="NAVIGATE" if direction else "WAIT",
            confidence=0.55,
            move_direction=direction,
            move_magnitude=mag,
            is_action_change=is_change,
            change_distance=change_dist,
            pressed_buttons=pressed,
        ))
    return frames


def _load_jsonl(path: Path) -> list[PerceptionSignal]:
    frames: list[PerceptionSignal] = []
    with path.open(encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            sig = parse_predict_response(rec)
            frames.append(sig)
    return frames


def simulate(frames: list[PerceptionSignal]) -> dict[str, int]:
    from unittest.mock import patch

    af = ActionFilter(confidence_threshold=0.4, action_change_threshold=0.15)
    gate = FastOutputGate()
    counts: Counter[str] = Counter()
    t = 0.0
    step = 1.0 / 2.5
    wall = [10_000.0]

    def _wall_time():
        return wall[0]

    with patch("backend.fast.action_filter.time.time", side_effect=_wall_time):
        for sig in frames:
            event = af.process(sig, t, global_min_interval=0.0)
            if event and event.trigger_fast:
                text = render_fast(event, GAME_ID)
                if gate.should_speak(event, text, GAME_ID, wall[0]):
                    key = f"P{event.fast_priority.value}_{event.type.value}"
                    counts[key] += 1
                    if event.fast_priority == FastPriority.SPELL:
                        counts["spell_text"] += 1
                    elif event.fast_priority == FastPriority.BUTTON:
                        counts["button_text"] += 1
                    elif event.fast_priority == FastPriority.DIRECTION:
                        counts["direction_text"] += 1
            t += step
            wall[0] += step

    return dict(counts)


def main():
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if path and path.exists():
        frames = _load_jsonl(path)
        source = str(path)
    else:
        frames = _synthetic_frames(FRAME_COUNT)
        source = f"synthetic×{FRAME_COUNT}"

    print(f"Source: {source}")
    print(f"Frames: {len(frames)}")
    stats = simulate(frames)
    print("\n--- Filter+Gate speak counts ---")
    for k in sorted(stats):
        print(f"  {k}: {stats[k]}")
    print(f"\nSpell (P0): {stats.get('spell_text', 0)}")
    print(f"Button (P1): {stats.get('button_text', 0)}")
    print(f"Direction (P3): {stats.get('direction_text', 0)}")


if __name__ == "__main__":
    main()
