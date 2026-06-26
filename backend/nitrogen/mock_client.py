"""
NitroGen 模拟客户端：无 ZMQ/GPU，向前端推送演示用 perception JSON。

用于仅测前端 + WebSocket + TTS/ASR 链路时的闭环验证。
"""

from __future__ import annotations
import logging
import threading
import time
from typing import TYPE_CHECKING, Optional

from backend.nitrogen.parser import PerceptionSignal

if TYPE_CHECKING:
    from backend.video.frame_pipe import VideoFramePipe

logger = logging.getLogger(__name__)

# 循环演示意图，便于调试面板看到变化
_DEMO_SIGNALS = (
    PerceptionSignal(
        primary_intent="DODGE",
        confidence=0.84,
        move_direction="LEFT",
        move_magnitude=0.72,
        horizon_sequence=["DODGE×6", "WAIT×4"],
    ),
    PerceptionSignal(
        primary_intent="ATTACK",
        confidence=0.79,
        move_direction=None,
        move_magnitude=0.0,
        horizon_sequence=["ATTACK×8", "NAVIGATE×2"],
    ),
    PerceptionSignal(
        primary_intent="WAIT",
        confidence=0.66,
        move_direction=None,
        move_magnitude=0.0,
        horizon_sequence=["WAIT×10"],
    ),
)


class MockNitroGenClient:
    """与 NitroGenClient 接口兼容，不连接 ZMQ。"""

    is_mock = True

    def __init__(self, cycle_sec: float = 0.35):
        self.cycle_sec = cycle_sec
        self._latest_signal: Optional[PerceptionSignal] = None
        self._signal_generation = 0
        self._signal_lock = threading.Lock()

        self._running = False
        self._paused = False
        self._thread: Optional[threading.Thread] = None

        self.inference_count = 0
        self.timeout_count = 0

    def start(self, frame_pipe: "VideoFramePipe"):
        self._running = True
        self._thread = threading.Thread(
            target=self._mock_loop,
            args=(frame_pipe,),
            daemon=True,
            name="nitrogen-mock",
        )
        self._thread.start()
        logger.info("MockNitroGenClient started (frontend-only mode, no ZMQ)")

    def pause(self):
        self._paused = True

    def resume(self):
        self._paused = False

    def stop(self):
        self._running = False
        logger.info("MockNitroGenClient stopped")

    @property
    def latest_signal(self) -> Optional[PerceptionSignal]:
        with self._signal_lock:
            return self._latest_signal

    def clear_signal(self):
        with self._signal_lock:
            self._latest_signal = None
            self._signal_generation += 1

    def on_frame_pushed(self):
        """前端推帧后立即更新模拟感知（探针/主应用无需等待后台轮询）"""
        with self._signal_lock:
            self._latest_signal = _DEMO_SIGNALS[self.inference_count % len(_DEMO_SIGNALS)]
        self.inference_count += 1

    def _mock_loop(self, frame_pipe: "VideoFramePipe"):
        idx = 0
        while self._running:
            if self._paused:
                time.sleep(0.05)
                continue

            if frame_pipe.latest_frame is None:
                time.sleep(0.02)
                continue

            template = _DEMO_SIGNALS[idx % len(_DEMO_SIGNALS)]
            idx += 1
            with self._signal_lock:
                gen = self._signal_generation
                self._latest_signal = template

            self.inference_count += 1
            time.sleep(self.cycle_sec)
