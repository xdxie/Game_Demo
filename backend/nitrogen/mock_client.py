"""
NitroGen 模拟客户端：输出 steer/throttle/brake 简化操控信号。
"""

from __future__ import annotations
import logging
import math
import threading
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Optional

from backend.nitrogen.controls import signal_from_controls
from backend.nitrogen.parser import PerceptionSignal

if TYPE_CHECKING:
    from backend.video.frame_pipe import VideoFramePipe

logger = logging.getLogger(__name__)

# (steer, throttle, brake) 演示序列
_DEMO_CONTROLS = (
    (-0.75, 1, 0),   # 左转 + 油门
    (0.0, 0, 1),     # 刹车
    (0.65, 1, 0),    # 右转 + 油门
    (0.0, 1, 0),     # 直行油门
    (-0.35, 1, 0),   # 微左
    (0.0, 0, 0),     # 滑行
)


class MockNitroGenClient:
    """与 NitroGenClient 接口兼容，不连接 ZMQ。"""

    is_mock = True

    def __init__(self, phase_sec: float = 2.5):
        self.phase_sec = phase_sec
        self._frame_pipe: Optional["VideoFramePipe"] = None
        self._latest_signal: Optional[PerceptionSignal] = None
        self._signal_generation = 0
        self._signal_lock = threading.Lock()

        self._running = False
        self._paused = False
        self._thread: Optional[threading.Thread] = None

        self.inference_count = 0
        self.timeout_count = 0

    def start(self, frame_pipe: "VideoFramePipe"):
        self._frame_pipe = frame_pipe
        self._running = True
        self._thread = threading.Thread(
            target=self._mock_loop,
            args=(frame_pipe,),
            daemon=True,
            name="nitrogen-mock",
        )
        self._thread.start()
        logger.info("MockNitroGenClient started (steer/throttle/brake, no ZMQ)")

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
        """推帧后按视频时间更新模拟操控量（不加速相位切换）。"""
        self._emit_demo_signal()

    def _control_index(self, video_time: float) -> int:
        return int(video_time / self.phase_sec) % len(_DEMO_CONTROLS)

    def _emit_demo_signal(self):
        pipe = self._frame_pipe
        video_time = pipe.video_position if pipe else 0.0
        idx = self._control_index(video_time)
        wobble = 0.08 * math.sin(video_time * 1.3)
        steer, throttle, brake = _DEMO_CONTROLS[idx]
        steer = max(-1.0, min(1.0, steer + wobble))

        signal = signal_from_controls(steer, throttle, brake)
        if brake:
            signal = replace(
                signal,
                primary_intent="DODGE",
                confidence=0.88,
                raw_dodge_score=0.9,
                hint_text="模拟闪避/刹车",
            )

        prev = self._latest_signal
        if prev is not None:
            delta = (
                abs(signal.steer - prev.steer)
                + abs(signal.throttle - prev.throttle)
                + abs(signal.brake - prev.brake)
            )
            if delta >= 0.2:
                hint = signal.hint_text or "操控变化"
                signal = replace(
                    signal,
                    is_action_change=True,
                    change_distance=delta,
                    hint_text=hint,
                )

        with self._signal_lock:
            if (
                self._latest_signal is not None
                and self._latest_signal.steer == signal.steer
                and self._latest_signal.throttle == signal.throttle
                and self._latest_signal.brake == signal.brake
            ):
                return
            self._latest_signal = signal
        self.inference_count += 1

    def _mock_loop(self, frame_pipe: "VideoFramePipe"):
        """低频保活：无推帧时仍按视频时间刷新信号。"""
        while self._running:
            if self._paused:
                time.sleep(0.05)
                continue

            if frame_pipe.latest_frame is None:
                time.sleep(0.05)
                continue

            self._emit_demo_signal()
            time.sleep(0.5)
