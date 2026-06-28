#!/usr/bin/env python3
"""
离线回放 NitroGen schema (3) JSON 目录 → ActionFilter → game_vocab TTS 统计。

用法:
  python tools/replay_raw_dump.py "d:\\Desktop\\f726c913...\\f726c913..."
  python tools/replay_raw_dump.py ./outputs --game black_myth_wukong --head 50
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.fast.action_filter import ActionFilter
from backend.fast.output_gate import FastOutputGate
from backend.fast.priority import FastPriority
from backend.fast.templates import render_fast
from backend.nitrogen.raw_chunk_adapter import AdapterState
from backend.nitrogen.fast_api_parser import parse_predict_response


def main() -> int:
    p = argparse.ArgumentParser(description="Replay raw_v3 JSON frames through fast TTS pipeline")
    p.add_argument("input_dir", type=Path, help="目录含 frame_*.json")
    p.add_argument("--game", default="black_myth_wukong", help="game_id for game_vocab")
    p.add_argument("--head", type=int, default=0, help="只处理前 N 帧，0=全部")
    p.add_argument("--fps", type=float, default=1.0 / 0.29, help="帧率用于 video_time")
    p.add_argument("--modifier-window", type=float, default=0.8)
    p.add_argument("--detail", action="store_true", help="打印每条约定的 TTS")
    args = p.parse_args()

    files = sorted(args.input_dir.glob("frame_*.json"))
    if args.head > 0:
        files = files[: args.head]
    if not files:
        print(f"no frame_*.json in {args.input_dir}", file=sys.stderr)
        return 1

    state = AdapterState()
    af = ActionFilter(modifier_window_sec=args.modifier_window)
    gate = FastOutputGate()
    dt = 1.0 / args.fps

    pri_counts: Counter = Counter()
    tts_counts: Counter = Counter()
    skip_counts: Counter = Counter()
    empty_text = 0
    spoken = 0

    import time
    wall_t = 1000.0

    for i, fp in enumerate(files):
        data = json.loads(fp.read_text(encoding="utf-8"))
        vt = i * dt
        sig = parse_predict_response(data, state=state)
        ev = af.process(sig, vt, global_min_interval=0.0)
        if ev is None:
            continue
        pri_counts[ev.fast_priority.name] += 1
        text = render_fast(ev, args.game)
        if not text:
            empty_text += 1
            if args.detail:
                print(f"{fp.name} vt={vt:.2f} {ev.type.value} pri={ev.fast_priority.name} -> (empty)")
            continue
        if not gate.should_speak(ev, text, args.game, wall_t):
            skip_counts["gate"] += 1
            if args.detail:
                print(f"{fp.name} vt={vt:.2f} SKIP gate '{text}'")
            wall_t += 0.05
            continue
        spoken += 1
        tts_counts[text] += 1
        wall_t += 1.5
        if args.detail:
            print(f"{fp.name} vt={vt:.2f} [{ev.fast_priority.name}] {text}")

    print(f"frames={len(files)} events={sum(pri_counts.values())} spoken={spoken}")
    print("priority:", dict(pri_counts))
    print("top_tts:", tts_counts.most_common(15))
    print(f"empty_text={empty_text} gate_skip={skip_counts['gate']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
