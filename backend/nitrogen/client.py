"""
NitroGen ZMQ 客户端。
封装与 serve.py 的 ZMQ REQ/REP 通信，在后台线程中异步推理，
对外暴露最新的 PerceptionSignal（始终是最新一帧，不排队）。
"""

from __future__ import annotations
import logging
import pickle
import threading
import time
from typing import TYPE_CHECKING, Optional

import zmq

from backend.nitrogen.parser import parse_chunk, PerceptionSignal

if TYPE_CHECKING:
    from backend.video.frame_pipe import VideoFramePipe

logger = logging.getLogger(__name__)


class NitroGenClient:
    """
    异步推理循环：主线程随时可以读 latest_signal，不阻塞。

    ZMQ 说明：
      - 使用 REQ/REP 模式（NitroGen serve.py 是 REP）
      - 每次发送 {"type": "predict", "image": PIL.Image}，返回 {"pred": chunk}
      - RCVTIMEO=2000ms，超时后重连
    """

    is_mock = False

    RECONNECT_DELAY = 2.0   # 超时后等待多久重连

    def __init__(self, server_addr: str = "tcp://localhost:5555",
                 btn_threshold: float = 0.5):
        self.server_addr = server_addr
        self.btn_threshold = btn_threshold

        self._ctx    = zmq.Context()
        self._socket: Optional[zmq.Socket] = None
        self._lock   = threading.Lock()

        self._latest_signal: Optional[PerceptionSignal] = None
        self._signal_generation = 0
        self._signal_lock = threading.Lock()

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._paused = False

        # 状态统计（调试用）
        self.inference_count = 0
        self.timeout_count   = 0

    # ── 生命周期 ──────────────────────────────────────────────────────

    def start(self, frame_pipe: "VideoFramePipe"):
        self._running = True
        self._connect()
        self._thread = threading.Thread(
            target=self._inference_loop,
            args=(frame_pipe,),
            daemon=True,
            name="nitrogen-inference",
        )
        self._thread.start()
        logger.info("NitroGenClient started → %s", self.server_addr)

    def pause(self):
        self._paused = True

    def resume(self):
        self._paused = False

    def stop(self):
        self._running = False
        if self._socket:
            self._socket.close()
        self._ctx.term()
        logger.info("NitroGenClient stopped")

    # ── 对外接口 ──────────────────────────────────────────────────────

    @property
    def latest_signal(self) -> Optional[PerceptionSignal]:
        with self._signal_lock:
            return self._latest_signal

    def clear_signal(self):
        """seek 时清空旧感知信号，并使进行中的推理结果失效"""
        with self._signal_lock:
            self._latest_signal = None
            self._signal_generation += 1

    # ── 内部推理循环 ──────────────────────────────────────────────────

    def _connect(self):
        if self._socket:
            self._socket.close()
        self._socket = self._ctx.socket(zmq.REQ)
        self._socket.setsockopt(zmq.RCVTIMEO, 2000)   # 2 秒超时
        self._socket.connect(self.server_addr)

    def _inference_loop(self, frame_pipe: "VideoFramePipe"):
        while self._running:
            if self._paused:
                time.sleep(0.05)
                continue

            frame = frame_pipe.latest_frame
            if frame is None:
                time.sleep(0.01)
                continue

            try:
                with self._signal_lock:
                    gen_at_start = self._signal_generation
                payload = pickle.dumps({"type": "predict", "image": frame})
                self._socket.send(payload)
                raw = self._socket.recv()
                response = pickle.loads(raw)

                chunk  = response["pred"]
                signal = parse_chunk(chunk, self.btn_threshold)

                with self._signal_lock:
                    if gen_at_start == self._signal_generation:
                        self._latest_signal = signal

                self.inference_count += 1

            except zmq.Again:
                # 超时，重连后重试
                logger.warning("NitroGen timeout（%d次），重连...", self.timeout_count)
                self.timeout_count += 1
                time.sleep(self.RECONNECT_DELAY)
                self._connect()

            except Exception as e:
                logger.error("NitroGen 推理异常：%s", e)
                time.sleep(0.5)
