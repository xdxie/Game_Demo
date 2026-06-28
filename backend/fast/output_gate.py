"""
快通道 TTS 输出门控：render_fast → TTS 之间的优先级与抑制层。

P0 法术 combo > P1 单键 > P2 意图 > P3 方向走位。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from backend.fast.event import EventType, GameEvent
from backend.fast.game_vocab import get_vocab
from backend.fast.priority import FastPriority
from backend.tts.queue import Priority

logger = logging.getLogger(__name__)

WUKONG_GAME_ID = "black_myth_wukong"
FORZA_GAME_ID = "forza_horizon_5"
WUKONG_REPEAT_TEXT_SEC = 2.5


@dataclass
class FastGateConfig:
    p0_cooldown_sec: float = 0.8
    p1_cooldown_sec: float = 1.2
    p3_cooldown_sec: float = 4.0
    wukong_p3_cooldown_sec: float = 25.0
    directional_suppress_sec: float = 3.0
    wukong_mag_threshold: float = 0.85
    forza_brake_cooldown_sec: float = 0.5
    wukong_heal_cooldown_sec: float = 3.0


class FastOutputGate:
    def __init__(self, cfg: FastGateConfig | None = None):
        self.cfg = cfg or FastGateConfig()
        self._last_p0: float = 0.0
        self._last_p1: float = 0.0
        self._last_p3: float = 0.0
        self._last_high_priority: float = 0.0
        self._last_p1_text: str = ""
        self._last_p1_text_at: float = 0.0

    def reset(self):
        self._last_p0 = 0.0
        self._last_p1 = 0.0
        self._last_p3 = 0.0
        self._last_high_priority = 0.0
        self._last_p1_text = ""
        self._last_p1_text_at = 0.0

    def priority(self,
                 event: GameEvent,
                 text: str,
                 game_id: str | None = None) -> FastPriority:
        if event.fast_priority is not None:
            return event.fast_priority
        if event.type == EventType.BUTTON_PRESS:
            if event.combo_keys:
                return FastPriority.SPELL
            return FastPriority.BUTTON
        if event.type in (EventType.ATTACK_WINDOW, EventType.SUSTAINED_DANGER):
            return FastPriority.INTENT
        return FastPriority.DIRECTION

    def _is_forza_brake(self, event: GameEvent, game_id: str | None) -> bool:
        return (
            game_id == FORZA_GAME_ID
            and event.type == EventType.BUTTON_PRESS
            and event.button_name == "LEFT_TRIGGER"
        )

    def _is_wukong_heal(self, event: GameEvent, game_id: str | None) -> bool:
        return (
            game_id == WUKONG_GAME_ID
            and event.type == EventType.BUTTON_PRESS
            and event.button_name == "LEFT_SHOULDER"
        )

    def should_speak(self,
                     event: GameEvent,
                     text: str,
                     game_id: str | None,
                     now: float | None = None) -> bool:
        if not text:
            logger.info("[快提示] 跳过: empty_text")
            return False

        now = now if now is not None else time.time()
        p = self.priority(event, text, game_id)

        if p == FastPriority.SPELL:
            self._last_high_priority = now
            return True

        if p == FastPriority.BUTTON:
            if (
                game_id == WUKONG_GAME_ID
                and text == self._last_p1_text
                and self._last_p1_text_at > 0
                and now - self._last_p1_text_at < WUKONG_REPEAT_TEXT_SEC
            ):
                logger.info(
                    "[快提示] 跳过: wukong_repeat_text (%.1fs < %.1fs)",
                    now - self._last_p1_text_at, WUKONG_REPEAT_TEXT_SEC,
                )
                return False

            p1_cd = self.cfg.p1_cooldown_sec
            if self._is_forza_brake(event, game_id):
                p1_cd = self.cfg.forza_brake_cooldown_sec
            elif self._is_wukong_heal(event, game_id):
                p1_cd = self.cfg.wukong_heal_cooldown_sec
            if self._last_p1 > 0 and now - self._last_p1 < p1_cd:
                logger.info(
                    "[快提示] 跳过: cooldown_p1 (%.1fs < %.1fs)",
                    now - self._last_p1, p1_cd,
                )
                return False
            self._last_p1 = now
            self._last_high_priority = now
            self._last_p1_text = text
            self._last_p1_text_at = now
            return True

        if p == FastPriority.DIRECTION:
            if (self._last_high_priority > 0
                    and now - self._last_high_priority < self.cfg.directional_suppress_sec):
                logger.info(
                    "[快提示] 跳过: directional_suppressed (距 P0/P1 %.1fs < %.1fs)",
                    now - self._last_high_priority, self.cfg.directional_suppress_sec,
                )
                return False

            vocab = get_vocab(game_id)
            if getattr(vocab, "suppress_directional_fast", False):
                mag = event.perception.move_magnitude
                if mag < self.cfg.wukong_mag_threshold:
                    logger.info(
                        "[快提示] 跳过: wukong_low_magnitude (%.2f < %.2f)",
                        mag, self.cfg.wukong_mag_threshold,
                    )
                    return False

            p3_cd = (
                self.cfg.wukong_p3_cooldown_sec
                if game_id == WUKONG_GAME_ID
                else self.cfg.p3_cooldown_sec
            )
            if self._last_p3 > 0 and now - self._last_p3 < p3_cd:
                logger.info(
                    "[快提示] 跳过: cooldown_p3 (%.1fs < %.1fs)",
                    now - self._last_p3, p3_cd,
                )
                return False
            self._last_p3 = now
            return True

        # P2 INTENT：沿用 P1 冷却，避免意图类刷屏
        if self._last_p1 > 0 and now - self._last_p1 < self.cfg.p1_cooldown_sec:
            logger.info("[快提示] 跳过: cooldown_intent")
            return False
        self._last_p1 = now
        return True

    def tts_priority(self, event: GameEvent, text: str) -> Priority:
        if self.priority(event, text) == FastPriority.SPELL:
            return Priority.FAST_SPELL
        return Priority.FAST_HINT
