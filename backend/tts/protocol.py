"""
TTS WebSocket 二进制帧协议。

服务端 → 客户端 TTS 音频帧：
  byte[0]    = 0x03（TTS 音频类型标识）
  byte[1:5]  = uint32 LE utterance_id
  byte[5:]   = MP3 bytes

将 utterance_id 绑定在音频帧上，消除 JSON 字幕与 MP3 到达乱序问题。
"""

from __future__ import annotations
import struct

TTS_AUDIO_PREFIX = 0x03
_HEADER_SIZE = 5  # 1 byte type + 4 bytes utterance_id


def frame_tts_audio(utterance_id: int, mp3_bytes: bytes) -> bytes:
    """将 MP3 打包为带 utterance_id 的 WebSocket 二进制帧"""
    return struct.pack("<BI", TTS_AUDIO_PREFIX, utterance_id) + mp3_bytes


def parse_tts_audio_frame(data: bytes) -> tuple[int, bytes] | None:
    """
    解析 TTS 音频帧。格式不合法时返回 None。
    """
    if len(data) < _HEADER_SIZE:
        return None
    if data[0] != TTS_AUDIO_PREFIX:
        return None
    utterance_id = struct.unpack_from("<I", data, 1)[0]
    return utterance_id, data[_HEADER_SIZE:]
