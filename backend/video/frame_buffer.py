"""
FrameBuffer：接收前端推送的视频帧，供 NitroGenClient 读取。

替代 VideoFramePipe 作为 NitroGen 的帧来源。
前端通过 WebSocket 以 10fps 发送 JPEG 帧 + 视频时间戳，后端解码后存入此 buffer。

接口与 VideoFramePipe 完全兼容（NitroGenClient 无需修改）：
  .latest_frame    Optional[PIL.Image]
  .video_position  float
  .pause() / .resume() / .seek()
"""

from __future__ import annotations
import io
import logging
from typing import Optional

from PIL import Image

logger = logging.getLogger(__name__)


class FrameBuffer:
    def __init__(self):
        self.latest_frame:   Optional[Image.Image] = None
        self.video_position: float = 0.0
        self.duration_sec:   float = 0.0   # 由前端 video_ready 消息设置
        self._paused = False

    def push(self, jpeg_bytes: bytes, video_time: float):
        """WebSocket 收到视频帧时调用（每帧约 100ms，10fps）"""
        if self._paused:
            return
        try:
            frame = Image.open(io.BytesIO(jpeg_bytes)).convert("RGB")
            self.latest_frame   = frame
            self.video_position = video_time
        except Exception as e:
            logger.warning("FrameBuffer decode error: %s", e)

    def pause(self):
        self._paused = True

    def resume(self):
        self._paused = False

    def seek(self, time: float):
        """同步视频时间；保留 latest_frame 以便暂停/seek 后仍可语音问答"""
        self.video_position = time
