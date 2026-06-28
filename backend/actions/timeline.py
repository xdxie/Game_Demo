"""
关键动作时间线 JSON 格式（v1，暂定）。

后续接实机 NitroGen 时保持字段不变，仅替换 source 与生成逻辑。
"""

from __future__ import annotations
import json
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class KeyAction:
    """单个关键动作快照（过滤后）。"""
    t_sec: float
    steer: float          # [-1, 1] 左右
    throttle: int         # 0 | 1
    brake: int            # 0 | 1
    intent: str           # NAVIGATE | DODGE | WAIT | ...
    confidence: float
    label: str = ""       # 简短机器标签，如 left_throttle / brake

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ActionTimeline:
    """整段视频的关键动作列表。"""
    version: int = 1
    source: str = "mock_nitrogen"
    duration_sec: float = 0.0
    sample_interval_sec: float = 2.0
    key_actions: list[KeyAction] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "source": self.source,
            "duration_sec": round(self.duration_sec, 3),
            "sample_interval_sec": self.sample_interval_sec,
            "key_actions": [a.to_dict() for a in self.key_actions],
        }

    def to_json(self, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    def summary_near(self, t_sec: float, window: float = 20.0) -> str:
        """供 VLM prompt 使用的窗口摘要。"""
        lo, hi = t_sec - window, t_sec + window
        items = [a for a in self.key_actions if lo <= a.t_sec <= hi]
        if not items:
            return "（该时间点附近无关键动作记录）"
        lines = []
        for a in items:
            lines.append(
                f"  t={a.t_sec:.1f}s intent={a.intent} conf={a.confidence:.0%} "
                f"[{a.label or '—'}]"
            )
        return "关键动作时间线（过滤后）:\n" + "\n".join(lines)
