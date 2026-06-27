"""
动作过滤器：消费 NitroGen 感知信号，检测关键动作和动作突变，输出 GameEvent。

所有阈值均为估算值，需要 2 号在真实视频上运行后调参。
参见 DESIGN.md 9.2 节和 TEAM.md 2号角色说明。

调参建议（2号）：
- 先打印每帧的 signal.primary_intent + confidence，观察分布
- 调整 fast_trigger_confidence，使 SUDDEN_DODGE 触发频率约 1~3 次/分钟
- 确认 ATTACK_WINDOW 触发时刻是否真正对应画面里的攻击机会
- 持续危险 sustained_danger_sec：太短会频繁触发，太长会错过提示时机
"""

from __future__ import annotations
import logging
import time
from typing import Optional

from backend.fast.event import EventType, GameEvent
from backend.nitrogen.parser import PerceptionSignal

logger = logging.getLogger(__name__)


class ActionFilter:
    """
    三层过滤：
    1. 结构变化检测（意图突变）
    2. 显著性门控（置信度阈值）
    3. 冷却时间（防止连续触发）
    """

    def __init__(self,
                 confidence_threshold: float = 0.75,
                 sustained_danger_sec: float = 3.0,
                 cooldowns: Optional[dict] = None):
        """
        Args:
            confidence_threshold: 快通道置信度下限（2号调参）
            sustained_danger_sec: DODGE 持续多久触发 SUSTAINED_DANGER（2号调参）
            cooldowns: 各事件类型冷却时间（秒），2号调参
        """
        self.confidence_threshold = confidence_threshold
        self.sustained_danger_sec = sustained_danger_sec

        self.COOLDOWNS: dict[EventType, float] = {
            EventType.SUDDEN_DODGE:      3.0,
            EventType.ATTACK_WINDOW:     4.0,
            EventType.SUSTAINED_DANGER:  8.0,
            EventType.MOVEMENT_SHIFT:   10.0,
            EventType.PATTERN_COMPLETED: 5.0,
        }
        if cooldowns:
            for k, v in cooldowns.items():
                try:
                    self.COOLDOWNS[EventType(k)] = v
                except ValueError:
                    pass

        self._last_trigger: dict[EventType, float] = {}
        self._prev_signal:  Optional[PerceptionSignal] = None

        # 持续状态追踪
        self._dodge_start:          float = 0.0
        self._current_pattern_type: str   = "WAIT"
        self._pattern_start:        float = 0.0

        # wall-clock timestamps (0.0 = never triggered)
        self._last_any_trigger: float = 0.0
        self._last_fast_trigger: float = 0.0

        # 诊断计数器
        self._signal_count: int = 0
        self._last_diag_wall: float = 0.0
        self._intent_counts: dict[str, int] = {}
        self._filtered_global: int = 0
        self._filtered_cooldown: int = 0

    def process(self,
                signal: PerceptionSignal,
                video_time: float,
                global_min_interval: float = 2.0
                ) -> Optional[GameEvent]:
        """
        每收到新的感知信号时调用（约 10fps）。
        返回 None 表示无需触发任何通道。
        冷却和间隔检查使用 wall-clock 而非 video_time，避免视频帧时间戳异常导致快系统沉默。
        """
        now = time.time()

        # 诊断：每 10 秒（wall-clock）打印意图分布
        self._signal_count += 1
        intent = signal.primary_intent or "UNKNOWN"
        self._intent_counts[intent] = self._intent_counts.get(intent, 0) + 1
        if self._last_diag_wall == 0.0:
            self._last_diag_wall = now
        elif now - self._last_diag_wall >= 10.0:
            dist = " ".join(f"{k}:{v}" for k, v in sorted(self._intent_counts.items()))
            logger.info(
                "ActionFilter 10s统计: signals=%d intents=[%s] conf=%.2f "
                "filtered(global=%d cooldown=%d) change=%s dir=%s vt=%.1f",
                self._signal_count, dist, signal.confidence,
                self._filtered_global, self._filtered_cooldown,
                signal.is_action_change, signal.move_direction,
                video_time,
            )
            self._intent_counts.clear()
            self._signal_count = 0
            self._filtered_global = 0
            self._filtered_cooldown = 0
            self._last_diag_wall = now

        event = self._detect(signal, video_time)

        if event is None:
            self._prev_signal = signal
            return None

        is_fast_only = event.trigger_fast and not event.trigger_slow

        # 全局最小间隔检查（wall-clock）：快系统独立计时器 + 较短间隔
        if is_fast_only:
            fast_interval = min(2.0, global_min_interval)
            if (self._last_fast_trigger > 0
                    and now - self._last_fast_trigger < fast_interval):
                self._filtered_global += 1
                logger.info(
                    "ActionFilter 快系统间隔过滤: %s (距上次快 %.1fs < %.1fs, vt=%.2f)",
                    event.type.value,
                    now - self._last_fast_trigger,
                    fast_interval,
                    video_time,
                )
                self._prev_signal = signal
                return None
        else:
            if (self._last_any_trigger > 0
                    and now - self._last_any_trigger < global_min_interval):
                self._filtered_global += 1
                logger.info(
                    "ActionFilter 全局间隔过滤: %s (距上次 %.1fs < %.1fs, vt=%.2f)",
                    event.type.value,
                    now - self._last_any_trigger,
                    global_min_interval,
                    video_time,
                )
                self._prev_signal = signal
                return None

        # 单类事件冷却检查（wall-clock）
        last = self._last_trigger.get(event.type, 0.0)
        cooldown = self.COOLDOWNS.get(event.type, 3.0)
        if last > 0 and now - last < cooldown:
            self._filtered_cooldown += 1
            logger.info(
                "ActionFilter 冷却过滤: %s (距上次 %.1fs < %.1fs, vt=%.2f)",
                event.type.value,
                now - last,
                cooldown,
                video_time,
            )
            self._prev_signal = signal
            return None

        # 通过所有检查，记录触发时间（wall-clock）
        self._last_trigger[event.type] = now
        if is_fast_only:
            self._last_fast_trigger = now
        else:
            self._last_any_trigger = now
        self._prev_signal = signal

        logger.info("ActionFilter 触发: %s @ vt=%.2f (conf=%.2f, fast=%s slow=%s)",
                     event.type.value, video_time, signal.confidence,
                     event.trigger_fast, event.trigger_slow)
        return event

    def reset(self):
        """视频 seek 时调用，重置帧间状态。
        清空冷却计时器，因为 seek 后场景完全改变。"""
        self._prev_signal           = None
        self._dodge_start           = 0.0
        self._current_pattern_type  = "WAIT"
        self._pattern_start         = 0.0
        self._last_trigger.clear()
        self._last_any_trigger      = 0.0
        self._last_fast_trigger     = 0.0

    # ── 内部检测逻辑 ──────────────────────────────────────────────────

    def _detect(self,
                signal: PerceptionSignal,
                t: float) -> Optional[GameEvent]:
        prev = self._prev_signal

        # ── 检测0：NitroGen / mock 动作突变（fast_api is_change）────────
        if (signal.is_action_change
                and signal.change_distance >= 0.05
                and signal.confidence >= self.confidence_threshold * 0.7):
            return self._make_event(EventType.MOVEMENT_SHIFT, t, signal,
                                    fast=True, slow=False)

        # ── 检测1：突发闪避 ────────────────────────────────────────────
        # 条件：当前意图是 DODGE，置信度超阈值，且上一帧不是 DODGE（突变）
        if (signal.primary_intent == "DODGE"
                and signal.confidence >= self.confidence_threshold
                and (prev is None or prev.primary_intent != "DODGE")):
            self._dodge_start = t
            return self._make_event(EventType.SUDDEN_DODGE, t, signal,
                                    fast=True, slow=False)

        # ── 检测2：攻击窗口开启 ────────────────────────────────────────
        # 条件：从 DODGE/GUARD 切换到 ATTACK，置信度超阈值
        if (signal.primary_intent == "ATTACK"
                and signal.confidence >= self.confidence_threshold * 0.93  # 攻击略宽松
                and prev is not None
                and prev.primary_intent in ("DODGE", "GUARD")):
            return self._make_event(EventType.ATTACK_WINDOW, t, signal,
                                    fast=True, slow=True)

        # ── 检测3：持续危险 ────────────────────────────────────────────
        # 条件：DODGE 意图持续超过 sustained_danger_sec
        if signal.primary_intent == "DODGE" and signal.confidence >= 0.6:
            if self._current_pattern_type == "DODGE":
                duration = t - self._dodge_start
                if duration >= self.sustained_danger_sec:
                    return self._make_event(EventType.SUSTAINED_DANGER, t, signal,
                                            fast=True, slow=True)
            else:
                self._dodge_start           = t
                self._current_pattern_type  = "DODGE"
        else:
            # 离开 DODGE 状态，重置计时
            if self._current_pattern_type == "DODGE":
                self._current_pattern_type = signal.primary_intent

        # ── 检测4：操作段结束（NitroGen 进入 WAIT/NAVIGATE）─────────
        # 条件：上一帧是战斗意图，当前帧切换到 WAIT/NAVIGATE
        non_combat = {"WAIT", "NAVIGATE"}
        was_combat = prev is not None and prev.primary_intent not in non_combat
        now_idle   = signal.primary_intent in non_combat

        if was_combat and now_idle:
            self._current_pattern_type = "WAIT"
            self._pattern_start = t
            return self._make_event(EventType.PATTERN_COMPLETED, t, signal,
                                    fast=False, slow=True)

        # ── 检测5：移动方向突变 ────────────────────────────────────────
        if (prev is not None
                and signal.move_direction is not None
                and prev.move_direction is not None
                and signal.move_direction != prev.move_direction
                and signal.move_magnitude > 0.5):
            return self._make_event(EventType.MOVEMENT_SHIFT, t, signal,
                                    fast=True, slow=False)

        return None

    @staticmethod
    def _make_event(evt_type: EventType, t: float,
                    signal: PerceptionSignal,
                    fast: bool, slow: bool) -> GameEvent:
        return GameEvent(
            type=evt_type,
            timestamp=t,
            perception=signal,
            trigger_fast=fast,
            trigger_slow=slow,
        )
