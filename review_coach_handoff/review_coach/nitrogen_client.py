"""
NitroGenClient — 调用远端 NitroGen 推理服务，输出直接对接 ActionSequenceSummarizer。

典型流程
────────
    from review_coach import NitroGenClient, ActionSequenceSummarizer, ReviewCoach, ReviewRequest

    with NitroGenClient() as client:
        clip = client.predict_clip(
            frames=frame_paths,          # list[Path | bytes]
            clip_start_sec=4.0,
            fps=10.0,
        )

    summary = clip.summarize()           # {action_summary, action_features, change_info}
    payload  = {**base_payload, **summary}
    request  = ReviewRequest.from_payload(payload)
    result   = ReviewCoach().generate(request)

SSH 隧道说明
─────────────
默认通过 SSH 端口转发把 localhost:8000 映射到远端 GPU 机器的 8000 端口。
设 auto_tunnel=False 可跳过，适用于已手动开好隧道或服务可直接访问的情况。
"""
from __future__ import annotations

import atexit
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import requests

from review_coach.action_sequence_summarizer import (
    ActionSequenceSummarizer,
    ActionSequenceInput,
)


# ── SSH / server defaults ─────────────────────────────────────────────────────

SSH_HOST    = "connect.bjb1.seetacloud.com"
SSH_PORT    = 18037
SSH_USER    = "root"
LOCAL_PORT  = 8000
REMOTE_PORT = 8000


# ── ClipResult ────────────────────────────────────────────────────────────────

@dataclass
class ClipResult:
    """
    一段 clip 的原始推理结果，可直接转换为 ActionSequenceSummarizer 输入。

    Attributes
    ----------
    raw           : 每帧的服务端原始 JSON，顺序与 predict_clip() 输入一致。
    clip_start_sec: clip 在视频里的起始秒数。
    clip_end_sec  : clip 在视频里的结束秒数。
    fps           : 采样帧率。
    """

    raw: list[dict]
    clip_start_sec: float
    clip_end_sec: float
    fps: float

    def to_action_sequence(self) -> ActionSequenceInput:
        """把原始 JSON 列表转为 ActionSequenceInput，供手动调用 summarize。"""
        return ActionSequenceSummarizer.from_nitrogen_frames(
            self.raw, self.clip_start_sec, self.clip_end_sec, self.fps
        )

    def summarize(self) -> dict:
        """
        直接运行 ActionSequenceSummarizer，返回：
          {
            "action_summary":  str,   # ≤80 字中文描述
            "action_features": dict,  # main_movement / jump_count / risk_tags …
            "change_info":     dict,  # is_change / change_points
          }
        """
        seq = self.to_action_sequence()
        return ActionSequenceSummarizer().summarize(seq)

    # ── 迭代支持 ─────────────────────────────────────────────────────────────

    def __iter__(self) -> Iterator[dict]:
        return iter(self.raw)

    def __len__(self) -> int:
        return len(self.raw)

    def __repr__(self) -> str:
        return (
            f"ClipResult(frames={len(self.raw)}, "
            f"t=[{self.clip_start_sec:.2f}s, {self.clip_end_sec:.2f}s])"
        )


# ── NitroGenClient ─────────────────────────────────────────────────────────────

class NitroGenClient:
    """
    NitroGen 推理 HTTP 客户端，带可选 SSH 隧道管理。

    Parameters
    ----------
    base_url    : 推理服务地址，默认 http://localhost:8000（经 SSH 隧道）。
    auto_tunnel : True 时在 __enter__ 自动起 SSH 隧道，__exit__ 时自动关闭。
    timeout     : 单帧请求超时（秒），默认 60s。

    推荐用 with 语句::

        with NitroGenClient() as client:
            clip = client.predict_clip(frames, clip_start_sec=4.0)

    也可手动管理（已有外部隧道时）::

        client = NitroGenClient(auto_tunnel=False)
        clip = client.predict_clip(frames, clip_start_sec=4.0)
    """

    def __init__(
        self,
        base_url: str | None = None,
        auto_tunnel: bool = True,
        timeout: float = 60.0,
    ):
        self.base_url    = (base_url or f"http://localhost:{LOCAL_PORT}").rstrip("/")
        self.auto_tunnel = auto_tunnel
        self.timeout     = timeout
        self._tunnel_proc: subprocess.Popen | None = None

    # ── Context manager ───────────────────────────────────────────────────────

    def __enter__(self) -> "NitroGenClient":
        if self.auto_tunnel:
            self._tunnel_proc = _start_ssh_tunnel()
        return self

    def __exit__(self, *_) -> None:
        if self._tunnel_proc and self._tunnel_proc.poll() is None:
            self._tunnel_proc.terminate()
            try:
                self._tunnel_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._tunnel_proc.kill()
            self._tunnel_proc = None

    # ── Server control ────────────────────────────────────────────────────────

    def reset(self) -> dict:
        """POST /reset — 清空服务端帧历史，开启新会话。"""
        r = requests.post(f"{self.base_url}/reset", timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def info(self) -> dict:
        """GET /info — 查服务端状态（可选端点）。"""
        r = requests.get(f"{self.base_url}/info", timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    # ── Single-frame inference ────────────────────────────────────────────────

    def predict_frame(self, image: str | Path | bytes) -> dict:
        """
        POST /predict，发单帧，返回服务端原始 JSON。

        Parameters
        ----------
        image : 文件路径（str / Path）或原始图像 bytes（JPEG / PNG）。

        Returns
        -------
        dict，关键字段：frame_idx, action_summary, is_change, change_info
        """
        if isinstance(image, (str, Path)):
            p = Path(image)
            with open(p, "rb") as fh:
                files = {"file": (p.name, fh, "application/octet-stream")}
                r = requests.post(
                    f"{self.base_url}/predict", files=files, timeout=self.timeout
                )
        else:
            files = {"file": ("frame.jpg", image, "application/octet-stream")}
            r = requests.post(
                f"{self.base_url}/predict", files=files, timeout=self.timeout
            )
        r.raise_for_status()
        return r.json()

    # ── Clip inference ────────────────────────────────────────────────────────

    def predict_clip(
        self,
        frames: list[str | Path | bytes],
        clip_start_sec: float = 0.0,
        clip_end_sec: float | None = None,
        fps: float = 10.0,
        reset_session: bool = True,
    ) -> ClipResult:
        """
        连续发送多帧，返回 ClipResult（可直接 .summarize()）。

        Parameters
        ----------
        frames         : 帧列表，按视频时间顺序，每项可以是路径或 bytes。
        clip_start_sec : clip 在原视频里的起始秒数，用于时间戳对齐。
        clip_end_sec   : 结束秒数，None 时按 fps 自动推算。
        fps            : 采样帧率，用于给每帧补 timestamp_sec。
        reset_session  : 发第一帧前是否先 POST /reset，默认 True。

        Returns
        -------
        ClipResult — 内含 .raw list[dict]，可调 .summarize() 得到结构化摘要。
        """
        if reset_session:
            self.reset()

        raw_results: list[dict] = []
        for i, frame in enumerate(frames):
            result = self.predict_frame(frame)
            result["timestamp_sec"] = round(clip_start_sec + i / fps, 4)
            raw_results.append(result)

        t_end = clip_end_sec if clip_end_sec is not None else (
            clip_start_sec + len(frames) / fps
        )
        return ClipResult(
            raw=raw_results,
            clip_start_sec=clip_start_sec,
            clip_end_sec=t_end,
            fps=fps,
        )


# ── SSH tunnel helpers ────────────────────────────────────────────────────────

def _port_is_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _wait_for_port(host: str, port: int, total_timeout: float = 15.0) -> bool:
    deadline = time.time() + total_timeout
    while time.time() < deadline:
        if _port_is_open(host, port):
            return True
        time.sleep(0.3)
    return False


def _start_ssh_tunnel() -> subprocess.Popen | None:
    """启动 SSH 端口转发；已开放时直接复用。"""
    if _port_is_open("127.0.0.1", LOCAL_PORT):
        print(f"[tunnel] localhost:{LOCAL_PORT} already open — reusing.")
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
    print(f"[tunnel] {' '.join(cmd)}")
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)

    if not _wait_for_port("127.0.0.1", LOCAL_PORT, total_timeout=15.0):
        proc.terminate()
        raise RuntimeError(
            f"SSH tunnel did not come up on localhost:{LOCAL_PORT} within 15s. "
            "Check SSH credentials / network."
        )

    print(f"[tunnel] up on localhost:{LOCAL_PORT}")
    atexit.register(lambda: proc.terminate() if proc.poll() is None else None)
    return proc
