"""
VLM 请求生命周期管理器。
同时最多 1 个 VLM 请求 in-flight + 1 个 pending，
USER_QUESTION 可取消当前请求并插队。
"""

from __future__ import annotations
import asyncio
import logging
import time
from typing import TYPE_CHECKING, Optional

from backend.fast.event import EventType, GameEvent
from backend.slow.vlm_client import call_vlm

if TYPE_CHECKING:
    from PIL import Image
    from backend.slow.context_buffer import ContextBuffer, ConversationHistory, FastHistory
    from backend.tts.queue import TTSQueue, Priority

logger = logging.getLogger(__name__)


class VLMRequestManager:
    """
    管理慢系统 VLM 请求的完整生命周期。

    优先级映射（与 TTSQueue.Priority 对应）：
      USER_QUESTION    → USER_ANSWER  (最高)
      PATTERN_COMPLETED → SLOW_SUMMARY (最低)
      其他事件          → SLOW_ADVICE  (中)
    """

    VLM_DEDUP_SEC = 5.0   # 同类事件去重窗口（秒）

    def __init__(
        self,
        tts_queue: "TTSQueue",
        context_buffer: "ContextBuffer",
        fast_history: "FastHistory",
        conversation_history: "ConversationHistory",
        vlm_model: str = "claude-sonnet-4-6",
        vlm_max_tokens: int = 120,
    ):
        self._tts       = tts_queue
        self._ctx       = context_buffer
        self._fast_hist = fast_history
        self._conv_hist = conversation_history
        self._model     = vlm_model
        self._max_tokens = vlm_max_tokens

        self._current_task: Optional[asyncio.Task] = None
        self._pending: Optional[dict] = None

        self._last_event_type:  Optional[EventType] = None
        self._last_submit_time: float = 0.0

    async def submit(self, event: GameEvent, frame: "Image.Image"):
        """提交 VLM 请求（非阻塞，立即返回）"""
        from backend.tts.queue import Priority

        priority  = self._event_to_priority(event)
        is_user_q = (event.type == EventType.USER_QUESTION)
        now = time.time()

        # ── 去重（用户提问不去重）────────────────────────────────────
        if (not is_user_q
                and event.type == self._last_event_type
                and now - self._last_submit_time < self.VLM_DEDUP_SEC):
            logger.debug("VLM dedup skip: %s", event.type.value)
            return

        # ── USER_QUESTION：取消当前，清空慢通道 TTS 队列 ─────────────
        if is_user_q:
            if self._current_task and not self._current_task.done():
                self._current_task.cancel()
            self._pending = None
            self._tts.clear_by_priority(
                [Priority.SLOW_ADVICE, Priority.SLOW_SUMMARY]
            )

        # ── 快照上下文（防止异步竞态读到更新后的状态）──────────────────
        task_args = {
            "event":         event,
            "frame":         frame,
            "priority":      priority,
            "ctx_snapshot":  self._ctx.summarize(),
            "fast_recent":   self._fast_hist.get_recent_summary(event.timestamp),
            "conv_messages": self._conv_hist.to_messages() if is_user_q else [],
        }

        # ── 提交 ──────────────────────────────────────────────────────
        if self._current_task is None or self._current_task.done():
            self._current_task = asyncio.create_task(self._run(task_args))
        else:
            # 已有 in-flight：只保留优先级最高的 pending
            pending_pri = self._pending["priority"] if self._pending else 999
            if priority <= pending_pri:
                self._pending = task_args

    async def _run(self, args: dict):
        try:
            event    = args["event"]
            is_user_q = (event.type == EventType.USER_QUESTION)

            text = await call_vlm(
                event=event,
                frame=args["frame"],
                ctx_summary=args["ctx_snapshot"],
                last_fast_text=args["fast_recent"],
                user_question=event.user_text if is_user_q else "",
                conversation_history=args["conv_messages"],
                model=self._model,
                max_tokens=self._max_tokens,
            )

            self._last_event_type  = event.type
            self._last_submit_time = time.time()

            self._tts.push(text, args["priority"])

            # 用户问答写入对话历史
            if is_user_q:
                self._conv_hist.add_turn(event.user_text, text)

        except asyncio.CancelledError:
            logger.debug("VLM task cancelled: %s", args["event"].type.value)
            raise

        except Exception as e:
            logger.error("VLM call failed: %s", e)

        finally:
            # 处理 pending
            if self._pending:
                pending = self._pending
                self._pending = None
                self._current_task = asyncio.create_task(self._run(pending))

    async def cancel_all(self):
        """视频 seek 时调用：取消所有在途请求"""
        if self._current_task and not self._current_task.done():
            self._current_task.cancel()
            try:
                await asyncio.wait_for(self._current_task, timeout=1.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        self._pending = None

    @staticmethod
    def _event_to_priority(event: GameEvent) -> "Priority":
        from backend.tts.queue import Priority
        if event.type == EventType.USER_QUESTION:
            return Priority.USER_ANSWER
        if event.type == EventType.PATTERN_COMPLETED:
            return Priority.SLOW_SUMMARY
        return Priority.SLOW_ADVICE
