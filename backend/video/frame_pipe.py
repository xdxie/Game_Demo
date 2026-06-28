"""
视频帧管道：从视频文件按帧提取图像，与视频播放时间轴保持同步。

设计原则：
- 按 target_fps 提取帧（推荐 10fps，匹配 NitroGen 推理频率）
- latest_frame 始终是最新帧，NitroGen 推理循环直接读取，不排队等待
- video_position 是视频时间（秒），是整个系统的"真值时钟"
"""

from __future__ import annotations
import logging
import threading
import time
from typing import Callable, Optional

import cv2
import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)


class VideoFramePipe:
    def __init__(self, video_path: str, target_fps: float = 10.0):
        """
        Args:
            video_path: 视频文件路径
            target_fps: 向下游推送的帧率（10fps 足够，降低推理负载）
        """
        self.video_path  = video_path
        self.target_fps  = target_fps
        self.frame_interval = 1.0 / target_fps

        self.cap: Optional[cv2.VideoCapture] = None
        self.native_fps: float = 30.0
        self.total_frames: int = 0
        self.duration_sec: float = 0.0

        # 对外暴露的最新状态（线程安全读）
        self.latest_frame: Optional[Image.Image] = None
        self.video_position: float = 0.0    # 视频时间（秒）

        self._running = False
        self._paused  = False
        self._thread: Optional[threading.Thread] = None
        self._lock    = threading.Lock()

        # 回调
        self._on_frame_cb: Optional[Callable] = None
        self._on_end_cb:   Optional[Callable] = None

    def open(self):
        """打开视频文件，读取元数据"""
        self.cap = cv2.VideoCapture(self.video_path)
        if not self.cap.isOpened():
            raise RuntimeError(f"无法打开视频文件：{self.video_path}")
        self.native_fps   = self.cap.get(cv2.CAP_PROP_FPS) or 30.0
        self.total_frames = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT))
        self.duration_sec = self.total_frames / self.native_fps
        logger.info("Video opened: %s  %.1ffps  %.1fs",
                    self.video_path, self.native_fps, self.duration_sec)

    def start(self,
              on_frame: Optional[Callable[[Image.Image, float], None]] = None,
              on_end:   Optional[Callable[[], None]] = None):
        """启动后台帧提取线程"""
        self._on_frame_cb = on_frame
        self._on_end_cb   = on_end
        self._running     = True
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="frame-pipe"
        )
        self._thread.start()

    def pause(self):
        self._paused = True

    def resume(self):
        self._paused = False

    def stop(self):
        self._running = False

    def seek(self, time_sec: float):
        """前端拖动进度条时调用（线程安全）"""
        if self.cap is None:
            return
        frame_idx = int(time_sec * self.native_fps)
        with self._lock:
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
            self.video_position = time_sec
        logger.info("Video seek → %.2fs", time_sec)

    # ── 内部帧提取循环 ────────────────────────────────────────────────

    def _loop(self):
        if self.cap is None:
            self.open()

        step = max(1, int(self.native_fps / self.target_fps))
        frame_idx = 0

        while self._running:
            if self._paused:
                time.sleep(0.05)
                continue

            with self._lock:
                ret, bgr = self.cap.read()

            if not ret:
                logger.info("Video ended")
                if self._on_end_cb:
                    self._on_end_cb()
                break

            # 按 target_fps 降采样：跳过不需要的帧
            frame_idx += 1
            if frame_idx % step != 0:
                continue

            # BGR → RGB → PIL
            rgb   = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
            frame = Image.fromarray(rgb).resize((256, 256), Image.LANCZOS)

            # 读取当前视频位置
            pos_ms = self.cap.get(cv2.CAP_PROP_POS_MSEC)
            video_time = pos_ms / 1000.0

            self.latest_frame    = frame
            self.video_position  = video_time

            if self._on_frame_cb:
                self._on_frame_cb(frame, video_time)

            # 按目标帧率限速（简单 sleep，不用精确计时）
            time.sleep(self.frame_interval * 0.8)

    def close(self):
        self.stop()
        if self.cap:
            self.cap.release()
