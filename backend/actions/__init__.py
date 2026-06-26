"""视频帧 → NitroGen（mock）→ 关键动作时间线 JSON，供 VLM 使用。"""

from backend.actions.timeline import ActionTimeline, KeyAction

__all__ = ["ActionTimeline", "KeyAction"]
