"""
帧 → NitroGen 快系统预测 → 关键动作过滤。

支持 mock / HTTP fast_api（action_fast_system）/ 离线 mock 时间网格。
"""

from __future__ import annotations
import io
import logging
import math
from typing import Optional

from PIL import Image

from backend.actions.timeline import ActionTimeline, KeyAction
from backend.config import Config, get_config
from backend.nitrogen.controls import signal_from_controls
from backend.nitrogen.factory import nitrogen_backend, nitrogen_mock_enabled
from backend.nitrogen.parser import PerceptionSignal

logger = logging.getLogger(__name__)

_DEMO_PATTERN = (
    (-0.75, 1, 0, "left_throttle"),
    (0.0, 0, 1, "brake"),
    (0.65, 1, 0, "right_throttle"),
    (0.0, 1, 0, "straight"),
    (-0.3, 1, 0, "slight_left"),
    (0.0, 0, 0, "coast"),
)


def _timeline_source(cfg: Config) -> str:
    if nitrogen_mock_enabled(cfg):
        return "mock_nitrogen"
    backend = nitrogen_backend(cfg)
    if backend == "fast_api":
        return "nitrogen_fast_api"
    return "nitrogen_zmq"


def signal_to_key_action(signal: PerceptionSignal, t_sec: float) -> KeyAction:
    label = (signal.hint_text or signal.primary_intent)[:48]
    return KeyAction(
        t_sec=round(t_sec, 3),
        steer=signal.steer,
        throttle=signal.throttle,
        brake=signal.brake,
        intent=signal.primary_intent,
        confidence=signal.confidence,
        label=label,
    )


def mock_predict_from_time(t_sec: float) -> KeyAction:
    idx = int(t_sec / 2.0) % len(_DEMO_PATTERN)
    steer, throttle, brake, label = _DEMO_PATTERN[idx]
    wobble = 0.12 * math.sin(t_sec * 1.3)
    steer = max(-1.0, min(1.0, steer + wobble))
    sig = signal_from_controls(steer, throttle, brake)
    return KeyAction(
        t_sec=round(t_sec, 3),
        steer=sig.steer,
        throttle=sig.throttle,
        brake=sig.brake,
        intent=sig.primary_intent,
        confidence=sig.confidence,
        label=label,
    )


def predict_from_jpeg(jpeg_bytes: bytes, t_sec: float, cfg: Config | None = None) -> KeyAction:
    cfg = cfg or get_config()
    if nitrogen_mock_enabled(cfg):
        try:
            Image.open(io.BytesIO(jpeg_bytes)).convert("RGB")
        except Exception as e:
            logger.warning("actions pipeline: bad jpeg at t=%.2f: %s", t_sec, e)
        return mock_predict_from_time(t_sec)

    if nitrogen_backend(cfg) == "fast_api":
        from backend.nitrogen.fast_api_client import predict_jpeg_bytes
        from backend.nitrogen.fast_api_parser import parse_predict_response

        try:
            data = predict_jpeg_bytes(
                jpeg_bytes,
                base_url=cfg.nitrogen_fast_api_url,
                timeout_sec=cfg.nitrogen_fast_api_timeout_sec,
            )
            return signal_to_key_action(parse_predict_response(data), t_sec)
        except Exception as e:
            logger.warning("fast_api timeline predict failed at t=%.2f: %s", t_sec, e)

    return mock_predict_from_time(t_sec)


def _is_key_action(candidate: KeyAction, prev: Optional[KeyAction]) -> bool:
    if prev is None:
        return True
    if candidate.brake == 1 and prev.brake == 0:
        return True
    if candidate.throttle != prev.throttle:
        return True
    if abs(candidate.steer - prev.steer) >= 0.35:
        return True
    if candidate.intent != prev.intent and candidate.confidence >= 0.7:
        return True
    if "动作变化" in (candidate.label or ""):
        return True
    return False


def build_timeline_from_samples(
    samples: list[tuple[float, Optional[bytes]]],
    duration_sec: float,
    sample_interval_sec: float = 2.0,
    min_gap_sec: float = 2.0,
    cfg: Config | None = None,
) -> ActionTimeline:
    cfg = cfg or get_config()
    source = _timeline_source(cfg)
    timeline = ActionTimeline(
        source=source,
        duration_sec=duration_sec,
        sample_interval_sec=sample_interval_sec,
    )
    last_kept: Optional[KeyAction] = None
    last_kept_t = -999.0

    reset_session = source == "nitrogen_fast_api"
    for i, (t_sec, jpeg) in enumerate(sorted(samples, key=lambda x: x[0])):
        if jpeg:
            if reset_session and i == 0:
                from backend.nitrogen.fast_api_client import predict_jpeg_bytes
                try:
                    data = predict_jpeg_bytes(
                        jpeg,
                        base_url=cfg.nitrogen_fast_api_url,
                        timeout_sec=cfg.nitrogen_fast_api_timeout_sec,
                        reset=True,
                    )
                    from backend.nitrogen.fast_api_parser import parse_predict_response
                    raw = signal_to_key_action(parse_predict_response(data), t_sec)
                except Exception as e:
                    logger.warning("fast_api timeline reset batch failed: %s", e)
                    raw = predict_from_jpeg(jpeg, t_sec, cfg)
            else:
                raw = predict_from_jpeg(jpeg, t_sec, cfg)
        else:
            raw = mock_predict_from_time(t_sec)

        if not _is_key_action(raw, last_kept):
            continue
        if t_sec - last_kept_t < min_gap_sec and last_kept is not None:
            continue
        timeline.key_actions.append(raw)
        last_kept = raw
        last_kept_t = t_sec

    logger.info(
        "Action timeline built: %d key actions from %d samples (%s)",
        len(timeline.key_actions),
        len(samples),
        source,
    )
    return timeline


def build_mock_timeline(duration_sec: float, interval: float = 2.0) -> ActionTimeline:
    samples = [(t, None) for t in _time_grid(duration_sec, interval)]
    return build_timeline_from_samples(samples, duration_sec, interval)


def _time_grid(duration_sec: float, interval: float) -> list[float]:
    if duration_sec <= 0:
        return [0.0]
    n = max(1, int(duration_sec / interval) + 1)
    return [round(i * interval, 3) for i in range(n) if i * interval <= duration_sec]
