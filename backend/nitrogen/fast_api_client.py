"""
NitroGen 快系统 HTTP 客户端（action_fast_system 远端 FastAPI）。

接口与 NitroGenClient / MockNitroGenClient 兼容：后台线程按帧率 POST /predict。
SSH 隧道由用户在本地预先建立（见 action_fast_system/README.md）。
"""

from __future__ import annotations
import io
import logging
import threading
import time
from typing import TYPE_CHECKING, Optional

import httpx
from PIL import Image

from backend.nitrogen.fast_api_parser import parse_predict_response
from backend.nitrogen.parser import PerceptionSignal

if TYPE_CHECKING:
    from backend.video.frame_pipe import VideoFramePipe

logger = logging.getLogger(__name__)


def predict_jpeg_bytes(
    jpeg_bytes: bytes,
    *,
    base_url: str,
    timeout_sec: float = 60.0,
    reset: bool = False,
) -> dict:
    """单帧推理（供时间线批处理等同步调用）。"""
    base = base_url.rstrip("/")
    timeout = httpx.Timeout(timeout_sec, connect=3.0)
    with httpx.Client(timeout=timeout) as client:
        if reset:
            client.post(f"{base}/reset")
        files = {"file": ("frame.jpg", jpeg_bytes, "image/jpeg")}
        resp = client.post(f"{base}/predict", files=files)
        resp.raise_for_status()
        return resp.json()


def predict_pil_image(
    image: Image.Image,
    *,
    base_url: str,
    timeout_sec: float = 60.0,
) -> PerceptionSignal:
    buf = io.BytesIO()
    image.convert("RGB").save(buf, format="JPEG", quality=85)
    data = predict_jpeg_bytes(
        buf.getvalue(), base_url=base_url, timeout_sec=timeout_sec,
    )
    return parse_predict_response(data)


class FastApiNitroGenClient:
    """通过 HTTP /predict 调用远端 NitroGen 快系统。"""

    is_mock = False
    backend = "fast_api"

    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        target_fps: float = 2.5,
        timeout_sec: float = 60.0,
        reset_on_start: bool = True,
        btn_threshold: float = 0.5,
    ):
        self.base_url = base_url.rstrip("/")
        self.target_fps = max(0.5, target_fps)
        self.timeout_sec = timeout_sec
        self.reset_on_start = reset_on_start
        self.btn_threshold = btn_threshold

        self._frame_pipe: Optional["VideoFramePipe"] = None
        self._latest_signal: Optional[PerceptionSignal] = None
        self._signal_generation = 0
        self._signal_lock = threading.Lock()

        self._running = False
        self._paused = False
        self._thread: Optional[threading.Thread] = None
        self._infer_lock = threading.Lock()

        self.inference_count = 0
        self.timeout_count = 0
        self.error_count = 0
        self._last_error: Optional[str] = None
        self._last_ok_time: float = 0.0

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    def start(self, frame_pipe: "VideoFramePipe"):
        self._frame_pipe = frame_pipe
        self._running = True
        if self.reset_on_start:
            threading.Thread(
                target=self._reset_on_start_bg,
                daemon=True,
                name="nitrogen-fast-api-reset",
            ).start()
        self._thread = threading.Thread(
            target=self._inference_loop,
            args=(frame_pipe,),
            daemon=True,
            name="nitrogen-fast-api",
        )
        self._thread.start()
        logger.info(
            "FastApiNitroGenClient started → %s (%.1f fps max)",
            self.base_url, self.target_fps,
        )

    def pause(self):
        self._paused = True

    def resume(self):
        self._paused = False

    def stop(self):
        self._running = False
        logger.info("FastApiNitroGenClient stopped")

    @property
    def latest_signal(self) -> Optional[PerceptionSignal]:
        with self._signal_lock:
            return self._latest_signal

    def clear_signal(self):
        with self._signal_lock:
            self._latest_signal = None
            self._signal_generation += 1
        try:
            self._post_reset()
        except Exception as e:
            logger.warning("FastAPI NitroGen /reset on clear failed: %s", e)

    def on_frame_pushed(self):
        """推帧后立即推理一帧（受 _infer_lock 与 target_fps 限制）。"""
        pipe = self._frame_pipe
        if pipe is None or pipe.latest_frame is None:
            return
        self._predict_frame(pipe.latest_frame)

    def _reset_on_start_bg(self) -> None:
        try:
            self._post_reset()
        except Exception as e:
            logger.warning("FastAPI NitroGen /reset on start failed: %s", e)

    def _post_reset(self) -> None:
        timeout = httpx.Timeout(15.0, connect=10.0)
        with httpx.Client(timeout=timeout) as client:
            r = client.post(f"{self.base_url}/reset")
            r.raise_for_status()
            logger.info("FastAPI NitroGen /reset OK")

    def _predict_frame(self, frame: Image.Image) -> None:
        if not self._running:
            return
        with self._signal_lock:
            gen_at_start = self._signal_generation
        try:
            with self._infer_lock:
                data = predict_jpeg_bytes(
                    self._pil_to_jpeg(frame),
                    base_url=self.base_url,
                    timeout_sec=self.timeout_sec,
                )
            signal = parse_predict_response(data, self.btn_threshold)
            with self._signal_lock:
                if gen_at_start == self._signal_generation:
                    self._latest_signal = signal
            self.inference_count += 1
            self._last_error = None
            self._last_ok_time = time.time()
            if self.inference_count == 1:
                logger.info(
                    "FastAPI NitroGen first inference OK → %s intent=%s",
                    self.base_url, signal.primary_intent,
                )
        except httpx.TimeoutException:
            self.timeout_count += 1
            self._last_error = f"推理超时（>{self.timeout_sec}s）"
            logger.warning(
                "FastAPI NitroGen timeout (#%d) url=%s",
                self.timeout_count, self.base_url,
            )
        except httpx.ConnectError as e:
            self.error_count += 1
            self._last_error = (
                f"无法连接 {self.base_url}（请检查 SSH 隧道与远端服务）: {e}"
            )
            logger.error(
                "FastAPI NitroGen connect failed url=%s — "
                "请确认 SSH 隧道与 NITROGEN_FAST_API_URL（勿用陪玩 8000 端口）: %s",
                self.base_url, e,
            )
        except Exception as e:
            self.error_count += 1
            self._last_error = str(e)
            logger.error("FastAPI NitroGen predict error (%s): %s", self.base_url, e)

    def _inference_loop(self, frame_pipe: "VideoFramePipe"):
        interval = 1.0 / self.target_fps
        while self._running:
            if self._paused:
                time.sleep(0.05)
                continue
            frame = frame_pipe.latest_frame
            if frame is None:
                time.sleep(0.05)
                continue
            t0 = time.perf_counter()
            if not self._running:
                break
            self._predict_frame(frame)
            elapsed = time.perf_counter() - t0
            time.sleep(max(0.0, interval - elapsed))

    @staticmethod
    def _pil_to_jpeg(frame: Image.Image) -> bytes:
        buf = io.BytesIO()
        frame.convert("RGB").save(buf, format="JPEG", quality=85)
        return buf.getvalue()
