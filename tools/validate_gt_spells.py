#!/usr/bin/env python3
"""
对照视频 GT 时间节点，离线验证 schema(3) frame → TTS 命中率。

用法:
  python tools/validate_gt_spells.py "d:\\Desktop\\f726c913...\\f726c913..."
  python tools/validate_gt_spells.py ./frames --window 5
  python tools/validate_gt_spells.py ./frames --timeline
  python tools/validate_gt_spells.py ./frames --compare-timeline
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.fast.action_filter import ActionFilter
from backend.fast.templates import render_fast
from backend.nitrogen.raw_chunk_adapter import AdapterState
from backend.nitrogen.fast_api_parser import parse_predict_response

GAME_ID = "black_myth_wukong"
FRAME_SEC = 0.1
FULL_FPS = 1.0 / FRAME_SEC

DEFAULT_GT = [
    (14.0, "RT+WEST", "给我定！"),
    (19.0, "RT+SOUTH", "上吧孩儿们！"),
    (51.0, "LS", "疾跑！"),
    (54.0, "RT+NORTH", "聚形散气！"),
    (61.0, "LS", "疾跑！"),
    (68.0, "RT+EAST", "广智救我！"),
    (102.0, "RT+LT", "化身！"),
    (137.0, "RT+NORTH", "聚形散气！"),
]


@dataclass
class TtsHit:
    frame_idx: int
    video_time: float
    text: str
    pri: str


def _frame_step(subsample_fps: float | None) -> int:
    if subsample_fps is None or subsample_fps <= 0:
        return 1
    if subsample_fps >= FULL_FPS:
        return 1
    return max(1, round(FULL_FPS / subsample_fps))


def replay_tts_events(
    frame_dir: Path,
    max_frame: int,
    modifier_window: float,
    subsample_fps: float | None = None,
    use_timeline: bool = False,
) -> list[TtsHit]:
    state = AdapterState()
    af = ActionFilter(modifier_window_sec=modifier_window)
    hits: list[TtsHit] = []
    step = _frame_step(subsample_fps)

    for fi in range(0, max_frame + 1, step):
        fp = frame_dir / f"frame_{fi:04d}.json"
        if not fp.is_file():
            continue
        data = json.loads(fp.read_text(encoding="utf-8"))
        vt = fi * FRAME_SEC
        sig = parse_predict_response(data, state=state)
        ev = af.process(
            sig, vt,
            global_min_interval=0.0,
            game_id=GAME_ID if use_timeline else None,
            raw_chunk=data if use_timeline else None,
            replay_clock=True,
        )
        if ev is None or not ev.trigger_fast:
            continue
        text = render_fast(ev, GAME_ID)
        if text:
            hits.append(TtsHit(fi, vt, text, ev.fast_priority.name))
    return hits


def validate_gt(
    frame_dir: Path,
    gt_list: list[tuple[float, str, str]],
    window_frames: int,
    modifier_window: float,
    subsample_fps: float | None = None,
    use_timeline: bool = False,
) -> tuple[int, int]:
    max_fi = max(int(round(t / FRAME_SEC)) + window_frames for t, _, _ in gt_list)
    events = replay_tts_events(
        frame_dir, max_fi, modifier_window,
        subsample_fps=subsample_fps,
        use_timeline=use_timeline,
    )
    window_sec = window_frames * FRAME_SEC
    step = _frame_step(subsample_fps)
    mode_parts = []
    if use_timeline:
        mode_parts.append("timeline")
    else:
        mode_parts.append("legacy")
    if subsample_fps:
        mode_parts.append(f"subsample {subsample_fps}fps step={step}")
    else:
        mode_parts.append("full 10fps")
    mode = " ".join(mode_parts)

    print(f"Replayed through frame_{max_fi} ({max_fi * FRAME_SEC:.1f}s), "
          f"{len(events)} TTS events [{mode}]\n")

    ok = 0
    for t_sec, label, expected in gt_list:
        lo, hi = t_sec - window_sec, t_sec + window_sec
        matched = [e for e in events if lo <= e.video_time <= hi and e.text == expected]
        status = "HIT" if matched else "MISS"
        if matched:
            ok += 1
        best = matched[0] if matched else None
        detail = (
            f"  f{best.frame_idx} vt={best.video_time:.1f}s pri={best.pri}"
            if best
            else f"  (window {lo:.1f}s–{hi:.1f}s, got: "
                 f"{[e.text for e in events if lo <= e.video_time <= hi]})"
        )
        print(f"[{status}] {label} @ {t_sec}s expect {expected!r}")
        print(detail)

    print(f"\nSummary: {ok}/{len(gt_list)} hits (±{window_frames} frames)")
    return ok, len(gt_list)


def main() -> int:
    p = argparse.ArgumentParser(description="Validate GT spell times against frame dump")
    p.add_argument("input_dir", type=Path, help="含 frame_*.json 的目录")
    p.add_argument("--window", type=int, default=5, help="GT 前后帧窗口（默认 5 = ±0.5s）")
    p.add_argument("--modifier-window", type=float, default=0.8)
    p.add_argument("--subsample-fps", type=float, default=None)
    p.add_argument("--compare-fps", action="store_true")
    p.add_argument(
        "--timeline",
        action="store_true",
        help="黑猴 chunk 时间线精筛（game_id + raw_chunk）",
    )
    p.add_argument(
        "--compare-timeline",
        action="store_true",
        help="对比 legacy 聚合路径 vs timeline 路径",
    )
    args = p.parse_args()

    if not args.input_dir.is_dir():
        print(f"not a directory: {args.input_dir}", file=sys.stderr)
        return 1

    if args.compare_timeline:
        results: list[tuple[str, int, int]] = []
        for label, use_tl in (("legacy", False), ("timeline", True)):
            print("=" * 60)
            print(f"  mode = {label}")
            print("=" * 60)
            ok, total = validate_gt(
                args.input_dir, DEFAULT_GT, args.window, args.modifier_window,
                use_timeline=use_tl,
            )
            results.append((label, ok, total))
            print()
        print("Compare summary:")
        for label, ok, total in results:
            print(f"  {label:>8}: {ok}/{total}")
        return 0

    if args.compare_fps:
        results: list[tuple[str, int, int]] = []
        for fps in (2.5, 8.0, 10.0):
            print("=" * 60)
            print(f"  subsample-fps = {fps}")
            print("=" * 60)
            ok, total = validate_gt(
                args.input_dir, DEFAULT_GT, args.window, args.modifier_window,
                subsample_fps=fps if fps < FULL_FPS else None,
                use_timeline=args.timeline,
            )
            results.append((str(fps), ok, total))
            print()
        print("Compare summary:")
        for label, ok, total in results:
            print(f"  {label:>4} fps: {ok}/{total}")
        return 0

    ok, total = validate_gt(
        args.input_dir, DEFAULT_GT, args.window, args.modifier_window,
        subsample_fps=args.subsample_fps,
        use_timeline=args.timeline,
    )
    return 0 if ok == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
