"""
测试 backend/tts/protocol.py 二进制帧编解码。
"""

import struct

from backend.tts.protocol import (
    TTS_AUDIO_PREFIX,
    frame_tts_audio,
    parse_tts_audio_frame,
)


class TestTTSProtocol:
    def test_frame_and_parse_roundtrip(self):
        mp3 = b"\xff\xfb\x90\x00fake_mp3_data"
        framed = frame_tts_audio(42, mp3)
        assert framed[0] == TTS_AUDIO_PREFIX
        assert struct.unpack_from("<I", framed, 1)[0] == 42
        assert framed[5:] == mp3

        parsed = parse_tts_audio_frame(framed)
        assert parsed is not None
        uid, data = parsed
        assert uid == 42
        assert data == mp3

    def test_parse_rejects_short_buffer(self):
        assert parse_tts_audio_frame(b"\x03\x01\x02") is None

    def test_parse_rejects_wrong_prefix(self):
        assert parse_tts_audio_frame(b"\x02" + b"\x00" * 4 + b"mp3") is None

    def test_large_utterance_id(self):
        uid = 1_000_000
        mp3 = b"audio"
        framed = frame_tts_audio(uid, mp3)
        parsed = parse_tts_audio_frame(framed)
        assert parsed[0] == uid
