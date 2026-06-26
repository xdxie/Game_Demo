"""
测试 backend/tts/queue.py 和 backend/video/frame_buffer.py
"""

import io
import heapq
import time
import threading
import pytest
from unittest.mock import MagicMock

from backend.tts.queue import TTSQueue, Priority, TTSItem
from backend.video.frame_buffer import FrameBuffer


@pytest.fixture
def queue_idle(mock_tts_engine, mock_asr_handler):
    return TTSQueue(
        mock_tts_engine, mock_asr_handler,
        inter_gap=0.0, fallback_margin=0.0,
    )


def make_item(text, priority, age_sec=0.0, expire_sec=30.0):
    return TTSItem(
        priority=priority,
        enqueue_time=time.time() - age_sec,
        text=text,
        expire_sec=expire_sec,
    )


def finish_playback(q: TTSQueue, utterance_id: int = 1):
    """模拟前端 tts_done 回传"""
    q.on_client_tts_done(utterance_id)


class TestTTSQueueImmediate:
    def test_push_when_idle_speaks_immediately(self, queue_idle, mock_tts_engine):
        queue_idle.push("你好", Priority.FAST_HINT)
        mock_tts_engine.speak_async.assert_called_once()
        assert mock_tts_engine.speak_async.call_args[0][0] == "你好"

    def test_empty_text_not_pushed(self, queue_idle, mock_tts_engine):
        queue_idle.push("", Priority.FAST_HINT)
        mock_tts_engine.speak_async.assert_not_called()

    def test_asr_muted_before_speak(self, queue_idle, mock_asr_handler):
        queue_idle.push("测试", Priority.FAST_HINT)
        mock_asr_handler.mute.assert_called()

    def test_asr_unmuted_after_client_tts_done(self, queue_idle, mock_asr_handler):
        queue_idle.push("测试", Priority.FAST_HINT)
        finish_playback(queue_idle, 1)
        mock_asr_handler.unmute.assert_called()

    def test_utterance_id_increments(self, queue_idle, mock_tts_engine, mock_asr_handler):
        on_start = MagicMock()
        queue_idle.set_callbacks(on_start=on_start)
        queue_idle.push("第一条", Priority.FAST_HINT)
        finish_playback(queue_idle, 1)
        time.sleep(0.05)
        queue_idle.push("第二条", Priority.FAST_HINT)
        assert on_start.call_args_list[0][0][2] == 1
        assert on_start.call_args_list[1][0][2] == 2


class TestTTSQueuePriority:
    def test_user_answer_interrupts_current(self, mock_tts_engine, mock_asr_handler):
        mock_tts_engine.speak_async.side_effect = (
            lambda text, is_cancelled=None, on_dispatched=None, on_error=None: None
        )

        on_interrupt = MagicMock()
        q = TTSQueue(mock_tts_engine, mock_asr_handler,
                     inter_gap=0.0, fallback_margin=0.0)
        q.set_callbacks(on_interrupt=on_interrupt)
        q.push("慢通道建议", Priority.SLOW_ADVICE)
        mock_tts_engine.stop.reset_mock()

        q.push("用户回答", Priority.USER_ANSWER)
        mock_tts_engine.stop.assert_called()
        on_interrupt.assert_called_once_with(1)

    def test_priority_heap_ordering(self, mock_tts_engine, mock_asr_handler):
        speak_order = []
        utterance_ids = []

        def _speak(text, is_cancelled=None, on_dispatched=None, on_error=None):
            speak_order.append(text)
            utterance_ids.append(q._pending_done_id)
            if on_dispatched and not (is_cancelled and is_cancelled()):
                on_dispatched(0.1)

        mock_tts_engine.speak_async.side_effect = _speak

        q = TTSQueue(mock_tts_engine, mock_asr_handler,
                     inter_gap=0.0, fallback_margin=0.0)

        q._is_speaking = True
        for text, pri in [
            ("慢总结", Priority.SLOW_SUMMARY),
            ("快提示", Priority.FAST_HINT),
            ("慢建议", Priority.SLOW_ADVICE),
        ]:
            with q._lock:
                heapq.heappush(q._heap, make_item(text, pri))

        for uid in [1, 2, 3]:
            q._is_speaking = False
            q._speak_next()
            finish_playback(q, uid)

        assert speak_order == ["快提示", "慢建议", "慢总结"]


class TestTTSQueueExpiry:
    def test_expired_item_discarded(self, mock_tts_engine, mock_asr_handler):
        q = TTSQueue(mock_tts_engine, mock_asr_handler,
                     inter_gap=0.0, fallback_margin=0.0)
        with q._lock:
            heapq.heappush(q._heap, make_item("过期", Priority.FAST_HINT,
                                               age_sec=100.0, expire_sec=2.0))
        q._speak_next()
        mock_tts_engine.speak_async.assert_not_called()

    def test_non_expired_item_spoken(self, queue_idle, mock_tts_engine):
        queue_idle.push("新鲜提示", Priority.SLOW_ADVICE)
        mock_tts_engine.speak_async.assert_called_once()

    def test_custom_max_age_from_config(self, mock_tts_engine, mock_asr_handler):
        q = TTSQueue(
            mock_tts_engine, mock_asr_handler,
            inter_gap=0.0, fallback_margin=0.0,
            max_age={Priority.FAST_HINT: 0.5},
        )
        with q._lock:
            heapq.heappush(q._heap, make_item(
                "过期快提示", Priority.FAST_HINT, age_sec=1.0, expire_sec=0.5,
            ))
        q._speak_next()
        mock_tts_engine.speak_async.assert_not_called()


class TestTTSQueuePlaybackDone:
    def test_stale_tts_done_ignored(self, queue_idle, mock_asr_handler):
        queue_idle.push("测试", Priority.FAST_HINT)
        mock_asr_handler.unmute.reset_mock()
        queue_idle.on_client_tts_done(999)
        mock_asr_handler.unmute.assert_not_called()

    def test_fallback_triggers_unmute(self, mock_tts_engine, mock_asr_handler):
        mock_tts_engine.speak_async.side_effect = \
            lambda text, is_cancelled=None, on_dispatched=None, on_error=None: (
                on_dispatched(0.05) if on_dispatched else None
            )

        q = TTSQueue(mock_tts_engine, mock_asr_handler,
                     inter_gap=0.0, fallback_margin=0.05)
        q.push("测试", Priority.FAST_HINT)
        time.sleep(0.2)
        mock_asr_handler.unmute.assert_called()

    def test_synth_error_advances_queue(self, mock_tts_engine, mock_asr_handler):
        mock_tts_engine.speak_async.side_effect = \
            lambda text, is_cancelled=None, on_dispatched=None, on_error=None: (
                on_error() if on_error else None
            )

        on_end = MagicMock()
        q = TTSQueue(mock_tts_engine, mock_asr_handler,
                     inter_gap=0.0, fallback_margin=0.0)
        q.set_callbacks(on_end=on_end)
        q.push("测试", Priority.FAST_HINT)
        time.sleep(0.05)
        on_end.assert_called()


class TestTTSQueueClear:
    def test_clear_by_priority_removes_target(self, mock_tts_engine, mock_asr_handler):
        q = TTSQueue(mock_tts_engine, mock_asr_handler,
                     inter_gap=0.0, fallback_margin=0.0)
        q._is_speaking = True
        for text, pri in [
            ("慢建议", Priority.SLOW_ADVICE),
            ("快提示", Priority.FAST_HINT),
            ("用户回答", Priority.USER_ANSWER),
        ]:
            with q._lock:
                heapq.heappush(q._heap, make_item(text, pri))

        q.clear_by_priority([Priority.SLOW_ADVICE])
        texts = [item.text for item in q._heap]
        assert "慢建议" not in texts
        assert "快提示" in texts
        assert "用户回答" in texts

    def test_clear_and_stop_empties_heap(self, mock_tts_engine, mock_asr_handler):
        q = TTSQueue(mock_tts_engine, mock_asr_handler,
                     inter_gap=0.0, fallback_margin=0.0)
        q._is_speaking = True
        with q._lock:
            heapq.heappush(q._heap, make_item("测试", Priority.SLOW_ADVICE))
        on_interrupt = MagicMock()
        q.set_callbacks(on_interrupt=on_interrupt)
        q._pending_done_id = 7
        q.clear_and_stop(notify=True)
        assert len(q._heap) == 0
        mock_tts_engine.stop.assert_called()
        on_interrupt.assert_called_once_with(7)


class TestTTSCallbacks:
    def test_on_speak_start_called_with_utterance_id(self, mock_tts_engine, mock_asr_handler):
        on_start = MagicMock()
        mock_tts_engine.speak_async.side_effect = (
            lambda text, is_cancelled=None, on_dispatched=None, on_error=None: None
        )

        q = TTSQueue(mock_tts_engine, mock_asr_handler,
                     inter_gap=0.0, fallback_margin=0.0)
        q.set_callbacks(on_start=on_start)
        q.push("快提示文本", Priority.FAST_HINT)

        on_start.assert_called_once_with("快提示文本", "fast", 1)

    def test_on_speak_end_called_after_client_done(self, mock_tts_engine, mock_asr_handler):
        on_end = MagicMock()
        q = TTSQueue(mock_tts_engine, mock_asr_handler,
                     inter_gap=0.0, fallback_margin=0.0)
        q.set_callbacks(on_end=on_end)
        q.push("测试", Priority.FAST_HINT)
        finish_playback(q, 1)
        on_end.assert_called()

    def test_broadcast_audio_wrapped_by_queue(self, mock_tts_engine, mock_asr_handler):
        broadcast = MagicMock()
        q = TTSQueue(mock_tts_engine, mock_asr_handler,
                     inter_gap=0.0, broadcast_audio=broadcast)
        q.push("测试", Priority.FAST_HINT)
        mock_tts_engine.on_audio_data(b"mp3bytes")
        broadcast.assert_called_once_with(1, b"mp3bytes")


class TestTTSQueueStaleSynthesis:
    def test_speak_token_invalidates_inflight(self, mock_tts_engine, mock_asr_handler):
        """打断后 is_cancelled 应返回 True，阻止旧 utterance 继续"""
        cancelled_fns = []

        def capture_cancel(text, is_cancelled=None, on_dispatched=None, on_error=None):
            if is_cancelled:
                cancelled_fns.append(is_cancelled)

        mock_tts_engine.speak_async.side_effect = capture_cancel

        q = TTSQueue(mock_tts_engine, mock_asr_handler,
                     inter_gap=0.0, fallback_margin=0.0)
        q.push("慢通道", Priority.SLOW_ADVICE)
        assert len(cancelled_fns) == 1
        assert not cancelled_fns[0]()

        q._interrupt(notify=False)
        assert cancelled_fns[0]()


class TestTTSQueueConcurrency:
    def test_concurrent_push_only_one_speaker(self, mock_tts_engine, mock_asr_handler):
        """并发 push 时 speaker 门闩应保证仅一条 utterance 在播"""
        started = []
        start_barrier = threading.Barrier(2)

        def _speak(text, is_cancelled=None, on_dispatched=None, on_error=None):
            started.append(text)
            if on_dispatched and not (is_cancelled and is_cancelled()):
                on_dispatched(0.1)

        mock_tts_engine.speak_async.side_effect = _speak

        q = TTSQueue(mock_tts_engine, mock_asr_handler,
                     inter_gap=0.0, fallback_margin=0.0)

        def push_text(label):
            start_barrier.wait(timeout=1.0)
            q.push(label, Priority.FAST_HINT)

        t1 = threading.Thread(target=push_text, args=("A",))
        t2 = threading.Thread(target=push_text, args=("B",))
        t1.start()
        t2.start()
        t1.join(timeout=2.0)
        t2.join(timeout=2.0)

        assert len(started) == 1
        assert q._is_speaking is True

        finish_playback(q, 1)
        time.sleep(0.05)
        assert mock_tts_engine.speak_async.call_count == 2


class TestTTSQueueInterruptUnmute:
    def test_clear_and_stop_unmutes_asr(self, mock_tts_engine, mock_asr_handler):
        mock_tts_engine.speak_async.side_effect = (
            lambda text, is_cancelled=None, on_dispatched=None, on_error=None: None
        )
        q = TTSQueue(mock_tts_engine, mock_asr_handler,
                     inter_gap=0.0, fallback_margin=0.0)
        q.push("被动提示", Priority.FAST_HINT)
        mock_asr_handler.force_unmute.reset_mock()
        q.clear_and_stop(notify=False)
        mock_asr_handler.force_unmute.assert_called()

    def test_barge_in_interrupt_unmutes_and_clears_fast(self, mock_tts_engine, mock_asr_handler):
        mock_tts_engine.speak_async.side_effect = (
            lambda text, is_cancelled=None, on_dispatched=None, on_error=None: None
        )
        q = TTSQueue(mock_tts_engine, mock_asr_handler,
                     inter_gap=0.0, fallback_margin=0.0)
        q.push("快提示", Priority.FAST_HINT)
        mock_asr_handler.force_unmute.reset_mock()
        q.barge_in_interrupt()
        mock_asr_handler.force_unmute.assert_called()
        with q._lock:
            assert all(i.priority == Priority.USER_ANSWER for i in q._heap)


# ═══════════════════════════════════════════════════════════════════════
# FrameBuffer
# ═══════════════════════════════════════════════════════════════════════

def make_jpeg_bytes(width=256, height=256) -> bytes:
    from PIL import Image
    img = Image.new("RGB", (width, height), color=(100, 150, 200))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


class TestFrameBuffer:
    def test_push_updates_latest_frame(self):
        fb = FrameBuffer()
        fb.push(make_jpeg_bytes(), 1.5)
        assert fb.latest_frame is not None
        assert fb.video_position == 1.5

    def test_push_invalid_jpeg_ignored(self):
        fb = FrameBuffer()
        fb.push(b"not a jpeg", 1.0)
        assert fb.latest_frame is None

    def test_pause_blocks_push(self):
        fb = FrameBuffer()
        fb.pause()
        fb.push(make_jpeg_bytes(), 2.0)
        assert fb.latest_frame is None

    def test_resume_allows_push(self):
        fb = FrameBuffer()
        fb.pause()
        fb.resume()
        fb.push(make_jpeg_bytes(), 3.0)
        assert fb.latest_frame is not None

    def test_seek_clears_frame(self):
        fb = FrameBuffer()
        fb.push(make_jpeg_bytes(), 5.0)
        assert fb.latest_frame is not None
        fb.seek(10.0)
        assert fb.latest_frame is None
        assert fb.video_position == 10.0

    def test_initial_state(self):
        fb = FrameBuffer()
        assert fb.latest_frame is None
        assert fb.video_position == 0.0
        assert fb.duration_sec == 0.0

    def test_multiple_pushes_keep_latest_time(self):
        fb = FrameBuffer()
        fb.push(make_jpeg_bytes(), 1.0)
        fb.push(make_jpeg_bytes(), 2.0)
        assert fb.video_position == 2.0

    def test_pushed_frame_is_pil_image(self):
        from PIL import Image
        fb = FrameBuffer()
        fb.push(make_jpeg_bytes(), 1.0)
        assert isinstance(fb.latest_frame, Image.Image)
