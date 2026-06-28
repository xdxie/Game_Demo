#!/usr/bin/env python3
"""Run NitroGen remote inference on a sequence of frames.

Connects to the NitroGen FastAPI server running on the remote autodl box
via an SSH tunnel, sends each frame in ``inputs/`` to ``/predict`` as a
continuous session, saves the returned JSON per frame, and reports the
wall-clock latency of every call plus summary statistics.

Usage:
    python run_inference.py                       # process inputs/ -> outputs/
    python run_inference.py --input-dir foo --output-dir bar
"""

from __future__ import annotations

import argparse
import atexit
import json
import socket
import statistics
import subprocess
import sys
import time
from pathlib import Path

import requests


SSH_HOST = "connect.bjb1.seetacloud.com"
SSH_PORT = 18037
SSH_USER = "root"
LOCAL_PORT = 8000
REMOTE_PORT = 8000


def port_is_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_for_port(host: str, port: int, total_timeout: float = 15.0) -> bool:
    deadline = time.time() + total_timeout
    while time.time() < deadline:
        if port_is_open(host, port):
            return True
        time.sleep(0.3)
    return False


def start_ssh_tunnel() -> subprocess.Popen | None:
    """Spawn a foreground SSH tunnel as a child process we can clean up.

    We deliberately do NOT use ``-f`` (background) so the tunnel dies with us.
    """
    if port_is_open("127.0.0.1", LOCAL_PORT):
        print(f"[tunnel] localhost:{LOCAL_PORT} already open — reusing it.")
        return None

    cmd = [
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=30",
        "-p", str(SSH_PORT),
        "-N",
        "-L", f"{LOCAL_PORT}:localhost:{REMOTE_PORT}",
        f"{SSH_USER}@{SSH_HOST}",
    ]
    print(f"[tunnel] launching: {' '.join(cmd)}")
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )

    if not wait_for_port("127.0.0.1", LOCAL_PORT, total_timeout=15.0):
        proc.terminate()
        raise RuntimeError(
            f"SSH tunnel did not come up on localhost:{LOCAL_PORT} within 15s. "
            f"Check SSH credentials / network."
        )

    print(f"[tunnel] up on localhost:{LOCAL_PORT}")

    def _cleanup() -> None:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
            print("[tunnel] closed.")

    atexit.register(_cleanup)
    return proc


def reset_server(base_url: str, timeout: float = 15.0) -> None:
    r = requests.post(f"{base_url}/reset", timeout=timeout)
    r.raise_for_status()
    print(f"[server] /reset OK -> {r.text.strip()[:200]}")


def predict_one(base_url: str, image_path: Path, timeout: float = 60.0) -> tuple[dict, float]:
    """POST a single image and return (result_json, elapsed_seconds)."""
    t0 = time.perf_counter()
    with open(image_path, "rb") as f:
        files = {"file": (image_path.name, f, "application/octet-stream")}
        r = requests.post(f"{base_url}/predict", files=files, timeout=timeout)
    elapsed = time.perf_counter() - t0
    r.raise_for_status()
    return r.json(), elapsed


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--input-dir", default="inputs",
                   help="Folder containing the frames to send (default: inputs).")
    p.add_argument("--output-dir", default="outputs",
                   help="Folder to write per-frame JSON results into.")
    p.add_argument("--pattern", default="frame_*.jpg",
                   help="Glob pattern for frames inside --input-dir.")
    p.add_argument("--server", default=f"http://localhost:{LOCAL_PORT}",
                   help="Base URL of the NitroGen server (default uses SSH tunnel).")
    p.add_argument("--no-tunnel", action="store_true",
                   help="Skip launching the SSH tunnel (assume --server is reachable).")
    p.add_argument("--no-reset", action="store_true",
                   help="Skip the initial /reset (continue an existing session).")
    args = p.parse_args()

    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    frames = sorted(input_dir.glob(args.pattern))
    if not frames:
        print(f"No frames matching {args.pattern!r} in {input_dir}", file=sys.stderr)
        return 1

    print(f"[scan] found {len(frames)} frame(s) in {input_dir}:")
    for f in frames:
        print(f"       - {f.name}")

    if not args.no_tunnel:
        start_ssh_tunnel()

    base_url = args.server.rstrip("/")

    if not args.no_reset:
        reset_server(base_url)

    timings: list[float] = []
    results_summary: list[dict] = []

    print("\n[infer] sending frames continuously...")
    for idx, frame in enumerate(frames):
        try:
            result, elapsed = predict_one(base_url, frame)
        except Exception as e:
            print(f"  [{idx:>2}] {frame.name}  ERROR: {e}", file=sys.stderr)
            return 2

        timings.append(elapsed)

        out_json = output_dir / f"{frame.stem}.json"
        with open(out_json, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)

        is_change = result.get("is_change")
        frame_idx = result.get("frame_idx")
        print(f"  [{idx:>2}] {frame.name}  "
              f"time={elapsed*1000:7.1f} ms  "
              f"frame_idx={frame_idx}  is_change={is_change}  "
              f"-> {out_json}")

        results_summary.append({
            "file": frame.name,
            "elapsed_sec": elapsed,
            "frame_idx": frame_idx,
            "is_change": is_change,
        })

    print("\n[summary] per-frame latency (s):")
    for r in results_summary:
        print(f"  {r['file']:<20s}  {r['elapsed_sec']*1000:8.2f} ms")

    if timings:
        total = sum(timings)
        print(f"\n  frames : {len(timings)}")
        print(f"  total  : {total*1000:8.2f} ms")
        print(f"  mean   : {statistics.mean(timings)*1000:8.2f} ms")
        print(f"  median : {statistics.median(timings)*1000:8.2f} ms")
        print(f"  min    : {min(timings)*1000:8.2f} ms")
        print(f"  max    : {max(timings)*1000:8.2f} ms")
        if len(timings) > 1:
            print(f"  stdev  : {statistics.stdev(timings)*1000:8.2f} ms")
            print(f"  (first frame often slowest due to warmup)")

    stats_path = output_dir / "_timings.json"
    with open(stats_path, "w", encoding="utf-8") as f:
        json.dump({
            "frames": results_summary,
            "total_sec": sum(timings),
            "mean_sec": statistics.mean(timings) if timings else None,
            "median_sec": statistics.median(timings) if timings else None,
            "min_sec": min(timings) if timings else None,
            "max_sec": max(timings) if timings else None,
        }, f, indent=2, ensure_ascii=False)
    print(f"\n[saved] timings -> {stats_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
