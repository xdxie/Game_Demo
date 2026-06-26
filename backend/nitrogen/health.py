"""
NitroGen FastAPI 可达性探针：端口开放 ≠ 推理服务可用。
"""

from __future__ import annotations

import io
import logging
from typing import Any
from urllib.parse import urlparse

import httpx
from PIL import Image

from backend.nitrogen.ssh_tunnel import port_is_open

logger = logging.getLogger(__name__)

_MIN_JPEG = None


def _minimal_jpeg() -> bytes:
    global _MIN_JPEG
    if _MIN_JPEG is None:
        buf = io.BytesIO()
        Image.new("RGB", (64, 64), color=(32, 32, 32)).save(
            buf, format="JPEG", quality=70,
        )
        _MIN_JPEG = buf.getvalue()
    return _MIN_JPEG


def _host_port(base_url: str) -> tuple[str, int]:
    parsed = urlparse(base_url)
    host = parsed.hostname or "127.0.0.1"
    if parsed.port:
        port = parsed.port
    elif parsed.scheme == "https":
        port = 443
    else:
        port = 80
    return host, port


def check_fast_api_health(
    base_url: str,
    *,
    timeout_sec: float = 20.0,
    probe_predict: bool = True,
) -> dict[str, Any]:
    """
    检查 NitroGen FastAPI 是否真正可用。

    返回字段：ok, port_open, reset_ok, predict_ok, message, url, latency_ms
    """
    base = base_url.rstrip("/")
    host, port = _host_port(base)
    result: dict[str, Any] = {
        "ok": False,
        "url": base,
        "port_open": False,
        "reset_ok": False,
        "predict_ok": False,
        "message": "",
        "reset_latency_ms": None,
        "predict_latency_ms": None,
    }

    if not port_is_open(host, port, timeout=1.0):
        result["message"] = (
            f"localhost:{port} 未监听。"
            "请检查 NITROGEN_SSH_TUNNEL=1 与 NITROGEN_SSH_PASSWORD，"
            "或确认远端 action_change_server 已启动。"
        )
        return result

    result["port_open"] = True
    timeout = httpx.Timeout(timeout_sec, connect=10.0)

    try:
        with httpx.Client(timeout=timeout) as client:
            import time as _time

            t0 = _time.perf_counter()
            r = client.post(f"{base}/reset")
            reset_ms = (_time.perf_counter() - t0) * 1000.0
            result["reset_latency_ms"] = round(reset_ms, 1)

            if r.status_code != 200:
                result["message"] = (
                    f"POST /reset 返回 HTTP {r.status_code}（非 NitroGen FastAPI？）"
                )
                return result

            result["reset_ok"] = True

            if not probe_predict:
                result["ok"] = True
                result["message"] = "端口与 /reset 正常"
                return result

            t1 = _time.perf_counter()
            files = {"file": ("probe.jpg", _minimal_jpeg(), "image/jpeg")}
            pr = client.post(f"{base}/predict", files=files)
            predict_ms = (_time.perf_counter() - t1) * 1000.0
            result["predict_latency_ms"] = round(predict_ms, 1)

            if pr.status_code != 200:
                result["message"] = (
                    f"POST /predict 返回 HTTP {pr.status_code}: {pr.text[:120]}"
                )
                return result

            data = pr.json()
            if "action_summary" not in data and "frame_idx" not in data:
                result["message"] = (
                    "POST /predict 响应格式异常（缺少 action_summary/frame_idx）"
                )
                return result

            result["predict_ok"] = True
            result["ok"] = True
            result["message"] = (
                f"NitroGen 正常（reset {reset_ms:.0f}ms, predict {predict_ms:.0f}ms）"
            )
            return result

    except httpx.ConnectError as e:
        result["message"] = (
            f"端口已开但 HTTP 连接失败（可能不是 FastAPI 服务）: {e}"
        )
    except httpx.TimeoutException:
        result["message"] = (
            f"请求超时（>{timeout_sec}s）。远端 GPU 服务可能未启动或模型仍在加载。"
        )
    except Exception as e:
        result["message"] = f"探针异常: {e}"
        logger.exception("NitroGen health probe failed for %s", base)

    return result
