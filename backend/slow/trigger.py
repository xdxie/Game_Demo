"""
VLM 请求生命周期管理器。
同时最多 1 个 VLM 请求 in-flight + 1 个 pending，
USER_QUESTION 可取消当前请求并插队。
"""

from __future__ import annotations
import asyncio
import logging
import time
from typing import TYPE_CHECKING, Callable, Optional

from backend.fast.event import EventType, GameEvent
from backend.slow.vlm_factory import call_vlm

if TYPE_CHECKING:
    from PIL import Image
    from backend.slow.context_buffer import ContextBuffer, ConversationHistory, FastHistory, SlowSpokenHistory
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

    def __init__(
        self,
        tts_queue: "TTSQueue",
        context_buffer: "ContextBuffer",
        fast_history: "FastHistory",
        conversation_history: "ConversationHistory",
        slow_spoken_history: "SlowSpokenHistory | None" = None,
        vlm_model: str = "claude-sonnet-4-6",
        vlm_max_tokens: int = 120,
        get_seek_generation: Optional[Callable[[], int]] = None,
        get_actions_timeline_text: Optional[Callable[[float], str]] = None,
        vlm_dedup_sec: float = 5.0,
        on_busy_change: Optional[Callable[[bool], None]] = None,
        on_user_error: Optional[Callable[[str], None]] = None,
        min_busy_display_sec: float = 0.45,
        vlm_nitrogen_input: bool = False,
        game_name: str = "",
    ):
        self._tts       = tts_queue
        self._ctx       = context_buffer
        self._fast_hist = fast_history
        self._conv_hist = conversation_history
        self._slow_spoken = slow_spoken_history
        self._model     = vlm_model
        self._max_tokens = vlm_max_tokens
        self._get_seek_generation = get_seek_generation
        self._get_actions_timeline_text = get_actions_timeline_text
        self._vlm_dedup_sec = vlm_dedup_sec
        self._on_busy_change = on_busy_change
        self._on_user_error = on_user_error
        self._min_busy_display_sec = min_busy_display_sec
        self._vlm_nitrogen_input = vlm_nitrogen_input
        self._game_name = game_name

        self._current_task: Optional[asyncio.Task] = None
        self._pending: Optional[dict] = None

        self._last_event_type:  Optional[EventType] = None
        self._last_submit_time: float = 0.0

    async def submit(
        self,
        event: GameEvent,
        frame: "Image.Image",
        utterance_seek_gen: int | None = None,
    ):
        """提交 VLM 请求（非阻塞，立即返回）"""
        from backend.tts.queue import Priority

        if not self._is_seek_generation_valid(utterance_seek_gen):
            logger.debug(
                "VLM submit discarded (stale after seek): %s",
                event.type.value,
            )
            return

        priority  = self._event_to_priority(event)
        is_user_q = (event.type == EventType.USER_QUESTION)
        now = time.time()

        # ── 去重（用户提问不去重）────────────────────────────────────
        if (not is_user_q
                and event.type == self._last_event_type
                and now - self._last_submit_time < self._vlm_dedup_sec):
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
            "submit_at":     time.time(),          # wall clock，用于超时判断
            "ctx_snapshot":  self._ctx.summarize(),
            "fast_recent":   self._fast_hist.get_recent_summary(event.timestamp),
            "conv_messages": self._conv_hist.to_messages() if is_user_q else [],
            "slow_spoken":   self._slow_spoken.get_recent_texts() if self._slow_spoken else [],
            "utterance_seek_gen": utterance_seek_gen,
        }

        # ── 提交 ──────────────────────────────────────────────────────
        if self._current_task is None or self._current_task.done():
            self._current_task = asyncio.create_task(self._run(task_args))
            self._notify_busy(True)
        else:
            # 已有 in-flight：只保留优先级最高的 pending
            pending_pri = self._pending["priority"] if self._pending else 999
            if priority <= pending_pri:
                self._pending = task_args

    async def _run(self, args: dict):
        busy_since = time.time()
        try:
            if not self._is_seek_generation_valid(args.get("utterance_seek_gen")):
                logger.debug(
                    "VLM run discarded (stale after seek): %s",
                    args["event"].type.value,
                )
                return

            event    = args["event"]
            is_user_q = (event.type == EventType.USER_QUESTION)

            rule_text = self._try_rule_response(event)
            if rule_text:
                logger.info("ReviewCoach rule → TTS push [%s]: %s", args["priority"].name, rule_text[:60])
                self._tts.push(rule_text, args["priority"])
                if is_user_q:
                    self._conv_hist.add_turn(event.user_text, rule_text)
                self._last_event_type  = event.type
                self._last_submit_time = time.time()
                return

            actions_text = ""
            if self._vlm_nitrogen_input and self._get_actions_timeline_text:
                actions_text = self._get_actions_timeline_text(event.timestamp)

            ctx_snapshot = (
                args["ctx_snapshot"]
                if self._vlm_nitrogen_input
                else ""
            )

            text = await call_vlm(
                event=event,
                frame=args["frame"],
                ctx_summary=ctx_snapshot,
                last_fast_text=args["fast_recent"],
                actions_timeline_text=actions_text,
                user_question=event.user_text if is_user_q else "",
                conversation_history=args["conv_messages"],
                slow_spoken=args.get("slow_spoken", []),
                model=self._model,
                max_tokens=self._max_tokens,
                include_nitrogen=self._vlm_nitrogen_input,
            )

            if not self._is_seek_generation_valid(args.get("utterance_seek_gen")):
                logger.debug(
                    "VLM result discarded (stale after seek): %s",
                    event.type.value,
                )
                return

            self._last_event_type  = event.type
            self._last_submit_time = time.time()

            # 计算剩余预算传给 TTS 队列：总预算 4s（事件触发→TTS播出），用户提问不限时
            if is_user_q:
                tts_expire = None
            else:
                elapsed = time.time() - args.get("submit_at", busy_since)
                remaining = 4.0 - elapsed
                if remaining <= 0:
                    logger.info(
                        "VLM result discarded (too late %.1fs > 4s): %s: %s",
                        elapsed, event.type.value, text[:40],
                    )
                    return
                tts_expire = remaining
                logger.debug("VLM result budget: elapsed=%.1fs tts_expire=%.1fs", elapsed, tts_expire)

            logger.info("VLM result → TTS push [%s]: %s", args["priority"].name, text[:60])
            self._tts.push(text, args["priority"], expire_sec=tts_expire)

            # 用户问答写入对话历史
            if is_user_q:
                self._conv_hist.add_turn(event.user_text, text)

        except asyncio.CancelledError:
            logger.debug("VLM task cancelled: %s", args["event"].type.value)
            raise

        except Exception as e:
            logger.error("VLM call failed [%s]: %s", type(e).__name__, e)
            if args["event"].type == EventType.USER_QUESTION:
                if self._on_user_error:
                    try:
                        self._on_user_error(f"VLM 调用失败: {e}")
                    except Exception as cb_err:
                        logger.error("VLM on_user_error callback failed: %s", cb_err)
                self._tts.push("抱歉，我没听清，请再说一次。", args["priority"])

        finally:
            if self._pending:
                pending = self._pending
                self._pending = None
                self._current_task = asyncio.create_task(self._run(pending))
            else:
                elapsed = time.time() - busy_since
                remain = self._min_busy_display_sec - elapsed
                if remain > 0:
                    await asyncio.sleep(remain)
                self._current_task = None
                self._notify_busy(False)

    async def cancel_all(self):
        """视频 seek 时调用：取消所有在途请求"""
        if self._current_task and not self._current_task.done():
            self._current_task.cancel()
            try:
                await asyncio.wait_for(self._current_task, timeout=1.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        self._pending = None
        self._current_task = None
        self._last_event_type = None
        self._last_submit_time = 0.0
        self._notify_busy(False)

    def _notify_busy(self, busy: bool):
        if self._on_busy_change:
            try:
                self._on_busy_change(busy)
            except Exception as e:
                logger.error("VLM on_busy_change error: %s", e)

    def _is_seek_generation_valid(self, utterance_seek_gen: int | None) -> bool:
        if utterance_seek_gen is None:
            return True
        if self._get_seek_generation is None:
            return True
        return utterance_seek_gen == self._get_seek_generation()

    @staticmethod
    def _event_to_priority(event: GameEvent) -> "Priority":
        from backend.tts.queue import Priority
        if event.type == EventType.USER_QUESTION:
            return Priority.USER_ANSWER
        if event.type == EventType.PATTERN_COMPLETED:
            return Priority.SLOW_SUMMARY
        if event.type == EventType.GREETING:
            return Priority.SLOW_ADVICE
        return Priority.SLOW_ADVICE

    def _try_rule_response(self, event: GameEvent) -> str | None:
        if not self._game_name:
            return None
        query = getattr(event, "user_text", "") or ""
        if not query:
            return None
        try:
            from review_coach.review_coach import ReviewCoach
            from review_coach.schemas import ReviewRequest
            from backend.config import game_type_for

            game_type = game_type_for(self._game_name)
            coach = ReviewCoach()
            skill = coach._select_skill(game_type)
            request = ReviewRequest(
                game_type=game_type,
                game_name=self._game_name,
                query=query,
                image_paths=[],
            )
            rule_result = skill.build_rule_response(request, "")
            if rule_result and rule_result.get("coaching_text"):
                logger.info(
                    "ReviewCoach rule hit [%s]: %s",
                    game_type, rule_result["coaching_text"][:40],
                )
                return rule_result["coaching_text"]
        except Exception as e:
            logger.debug("ReviewCoach rule check failed: %s", e)
        return None
