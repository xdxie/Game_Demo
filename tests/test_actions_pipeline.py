"""动作时间线 pipeline"""

from backend.actions.pipeline import build_mock_timeline, build_timeline_from_samples
from backend.actions.timeline import ActionTimeline


def test_build_mock_timeline_has_key_actions():
    tl = build_mock_timeline(20.0, interval=2.0)
    assert tl.duration_sec == 20.0
    assert len(tl.key_actions) >= 1
    a = tl.key_actions[0]
    assert -1.0 <= a.steer <= 1.0
    assert a.throttle in (0, 1)
    assert a.brake in (0, 1)


def test_timeline_summary_near():
    tl = build_mock_timeline(30.0, interval=2.0)
    text = tl.summary_near(10.0, window=15.0)
    assert "关键动作" in text or "无关键动作" in text


def test_ingest_batch_style_samples():
    samples = [(0.0, None), (2.0, None), (4.0, None), (6.0, None)]
    tl = build_timeline_from_samples(samples, duration_sec=8.0, sample_interval_sec=2.0)
    assert isinstance(tl, ActionTimeline)
    assert tl.to_dict()["version"] == 1
