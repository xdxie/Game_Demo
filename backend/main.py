"""
主入口：FastAPI + WebSocket 服务。

Fix 11：视频帧由前端 canvas 捕获后通过 WebSocket 推送，
        后端不再用 cv2 读取视频文件，使用 FrameBuffer 接收帧。
Fix 13：ASRHandler 使用独立转写线程，不阻塞 WebSocket 协程。
Fix 14：TTS 音频 bytes 通过 WebSocket 发送到前端播放。

WebSocket 二进制协议（客户端 → 服务端）：
  byte[0] = 0x01：PCM 音频（麦克风，用于 ASR）
            byte[1:] = PCM int16 LE

  byte[0] = 0x02：视频帧（canvas 截图，用于 NitroGen）
            byte[1:9] = float64 LE（视频当前时间，秒）
            byte[9:]  = JPEG bytes（256×256）

WebSocket 二进制协议（服务端 → 客户端）：
  byte[0]=0x03  TTS 音频：byte[1:5]=uint32 LE utterance_id，byte[5:]=MP3
"""

from __future__ import annotations
import asyncio
import logging
import struct
import time
from pathlib import Path
from typing import Optional

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env")

from backend.config import reload_config_from_env
reload_config_from_env()

_LOG_FILE = _ROOT / "session.log"

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
for _h in logging.getLogger().handlers:
    if isinstance(_h, logging.StreamHandler) and not isinstance(_h, logging.FileHandler):
        _h.setLevel(logging.INFO)
_file_handler = logging.FileHandler(str(_LOG_FILE), mode="w", encoding="utf-8")
_file_handler.setLevel(logging.DEBUG)
_file_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(name)s] %(levelname)s %(message)s"
))
logging.getLogger().addHandler(_file_handler)

logger = logging.getLogger(__name__)
logger.info("日志文件: %s (每次启动清空)", _LOG_FILE)


def _websocket_stack_ready() -> bool:
    """uvicorn 需要 websockets 或 wsproto 才能处理 /ws 升级"""
    try:
        import websockets  # noqa: F401
        return True
    except ImportError:
        try:
            import wsproto  # noqa: F401
            return True
        except ImportError:
            return False

from backend.config import get_config
from backend.video.frame_buffer import FrameBuffer          # Fix 11
from backend.nitrogen.factory import (
    create_nitrogen_client,
    nitrogen_analysis_fps,
    nitrogen_mock_enabled,
    nitrogen_mode_label,
)
from backend.fast.action_filter import ActionFilter
from backend.fast.templates import render_fast
from backend.fast.event import EventType, GameEvent
from backend.slow.context_buffer import ContextBuffer, ConversationHistory, FastHistory, SlowSpokenHistory
from backend.slow.trigger import VLMRequestManager
from backend.tts.engine import TTSEngine
from backend.tts.protocol import frame_tts_audio
from backend.tts.queue import TTSQueue, Priority
from backend.asr.handler import ASRHandler
from backend import warmup
from backend.actions.pipeline import build_mock_timeline, build_timeline_from_samples
from backend.actions.timeline import ActionTimeline
from backend.slow.vlm_factory import vlm_mock_enabled, vlm_provider

import os

app = FastAPI(title="陪玩")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

FRONTEND_DIR = _ROOT / "frontend"
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

_session: Optional["GameSession"] = None
_action_timeline: Optional[ActionTimeline] = None
_timeline_building: bool = False
_ws_clients: list[WebSocket] = []
_ws_roles: dict[WebSocket, str] = {}   # "player" | "observer"
_primary_ws: Optional[WebSocket] = None
_pcm_drop_logged: bool = False


@app.on_event("startup")
async def _on_startup():
    """服务启动即后台预热 Whisper/TTS，缩短首次「开始分析」等待。"""
    cfg = get_config()
    from backend.nitrogen.factory import nitrogen_mode_label
    if nitrogen_mode_label(cfg) == "fast_api":
        from backend.nitrogen.ssh_tunnel import ensure_nitrogen_ssh_tunnel
        try:
            ensure_nitrogen_ssh_tunnel(cfg.nitrogen_fast_api_url)
        except Exception as e:
            logger.warning("SSH tunnel auto-start failed: %s", e)
    await warmup.start_background_warmup(cfg)

    # VLM 连通性自检
    if vlm_provider(cfg) == "openai":
        loop = asyncio.get_running_loop()
        from backend.slow.vlm_openai import selftest as vlm_selftest
        try:
            ok = await loop.run_in_executor(None, vlm_selftest, cfg)
            if not ok:
                logger.error("VLM selftest FAILED — 慢系统将无法响应")
        except Exception as e:
            logger.error("VLM selftest exception: %s", e)

    if not _websocket_stack_ready():
        logger.warning(
            "websockets 未安装：/ws 将无法升级，麦克风与推帧均不可用。"
            "请执行: pip install websockets"
        )
    logger.info(
        "Startup: vlm=%s model=%s key=%s",
        vlm_provider(cfg),
        cfg.vlm_model,
        "set" if (cfg.vlm_api_key or os.getenv("VLM_API_KEY")) else "missing",
    )


@app.on_event("shutdown")
async def _on_shutdown():
    global _session
    if _session is not None:
        try:
            _session.nitrogen.stop()
        except Exception:
            pass
    from backend.nitrogen.ssh_tunnel import stop_ssh_tunnel
    stop_ssh_tunnel()
    import os
    os._exit(0)


def _reassign_primary_from_players() -> Optional[WebSocket]:
    """从仍在线的 player 角色连接中选举主连接。"""
    global _primary_ws
    if _primary_ws is not None and _primary_ws in _ws_clients:
        return _primary_ws
    _primary_ws = next(
        (w for w in _ws_clients if _ws_roles.get(w) == "player"),
        None,
    )
    return _primary_ws


def _remove_dead_ws_clients(dead: list[WebSocket]) -> None:
    """移除断开的 WebSocket；若主连接失效则提升下一个 player。"""
    global _primary_ws
    lost_primary = False
    for ws in dead:
        if ws in _ws_clients:
            _ws_clients.remove(ws)
        if _primary_ws is ws:
            lost_primary = True
        _ws_roles.pop(ws, None)
    if lost_primary or (_primary_ws is not None and _primary_ws not in _ws_clients):
        _primary_ws = None
        _reassign_primary_from_players()


async def _send_session_role(ws: WebSocket) -> None:
    role = "primary" if ws is _primary_ws else "observer"
    try:
        await ws.send_json({"type": "session_role", "role": role})
    except Exception:
        pass
    if role == "primary" and _session is not None:
        if _session._running and _session._analysis_paused:
            try:
                await _session.on_resume()
            except Exception as e:
                logger.warning("Resume on primary register failed: %s", e)
        try:
            await _session._broadcast_asr_state(ws)
        except Exception as e:
            logger.warning("ASR state on register failed: %s", e)


async def _handle_register(ws: WebSocket, data: dict) -> None:
    """客户端注册角色：首个 player 成为主连接，其余为旁观。"""
    global _primary_ws
    role = data.get("role", "observer")
    if role not in ("player", "observer"):
        role = "observer"
    _ws_roles[ws] = role

    if role == "player" and (
        _primary_ws is None or _primary_ws not in _ws_clients
    ):
        _primary_ws = ws
    elif _primary_ws is ws and role == "observer":
        _primary_ws = None
        _reassign_primary_from_players()

    await _send_session_role(ws)
    logger.info(
        "WS register role=%s primary=%s (total=%d)",
        role,
        ws is _primary_ws,
        len(_ws_clients),
    )


# ═══════════════════════════════════════════════════════════════════════
# GameSession
# ═══════════════════════════════════════════════════════════════════════

class GameSession:
    def __init__(self):
        cfg = get_config()
        self.cfg = cfg
        self._ws_clients = _ws_clients

        self._main_loop_task: Optional[asyncio.Task] = None
        self._running = False
        self._analysis_paused = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._pcm_chunk_count = 0
        self._video_frame_count = 0
        self._t0: float = 0.0
        self.current_game: str = "街头霸王6"
        self._first_frame_greeted = False

        self.frame_buffer = FrameBuffer()
        self.nitrogen = create_nitrogen_client(cfg)
        self.action_filter = ActionFilter(
            confidence_threshold=cfg.fast_trigger_confidence,
            sustained_danger_sec=cfg.sustained_danger_sec,
            cooldowns=cfg.cooldowns,
        )

        self.ctx_buffer = ContextBuffer(window_sec=cfg.context_window_sec)
        self.conv_hist  = ConversationHistory()
        self.fast_hist  = FastHistory()
        self.slow_spoken_hist = SlowSpokenHistory()

        global _action_timeline
        self.action_timeline: ActionTimeline = (
            _action_timeline
            if _action_timeline is not None
            else build_mock_timeline(0.0)
        )

        self.tts_engine  = TTSEngine(
            engine=cfg.tts_engine,
            voice=cfg.tts_voice,
            rate=cfg.tts_rate,
            volc_api_key=cfg.volc_api_key,
            volc_speaker_fast=cfg.volc_speaker_fast,
            volc_speaker_slow=cfg.volc_speaker_slow,
            volc_speed_ratio_fast=cfg.volc_speed_ratio_fast,
            volc_speed_ratio_slow=cfg.volc_speed_ratio_slow,
        )
        tts_cache = warmup.get_tts_cache()
        if tts_cache:
            self.tts_engine._cache.update(tts_cache)

        whisper = warmup.get_whisper_model(cfg)
        self.asr_handler = ASRHandler(
            model_size=cfg.whisper_model,
            language=cfg.whisper_language,
            engine=cfg.asr_engine,
            device=cfg.asr_device,
            vad_silence_threshold=cfg.vad_silence_threshold,
            vad_speech_min_sec=cfg.vad_speech_min_sec,
            vad_silence_end_sec=cfg.vad_silence_end_sec,
            vad_silence_end_short_sec=cfg.vad_silence_end_short_sec,
            vad_adaptive_boundary_sec=cfg.vad_adaptive_boundary_sec,
            vad_max_speech_sec=cfg.vad_max_speech_sec,
            tts_mute_tail_sec=cfg.tts_mute_tail_sec,
            barge_in_enabled=cfg.barge_in_enabled,
            barge_in_threshold_mult=cfg.barge_in_threshold_mult,
            whisper_model=whisper,
            asr_engine_type=warmup.get_asr_engine_type(cfg),
        )
        self.asr_handler.on_state_change = self._on_asr_state_change
        self.asr_handler.on_barge_in = self._on_asr_barge_in
        self.asr_handler._emit_state()
        self.asr_handler.is_tts_playing = lambda: self.tts_queue.is_speaking
        self.tts_queue = TTSQueue(
            tts_engine=self.tts_engine,
            asr_handler=self.asr_handler,
            inter_gap=cfg.tts_inter_utterance_gap,
            user_inter_gap=cfg.tts_user_inter_gap,
            fallback_margin=cfg.tts_done_fallback_margin,
            broadcast_audio=self._broadcast_tts_audio,
            max_age={
                Priority.USER_ANSWER:  30.0,
                Priority.FAST_HINT:    cfg.fast_hint_expire_sec,
                Priority.SLOW_ADVICE:  cfg.slow_max_queue_age,
                Priority.SLOW_SUMMARY: cfg.slow_max_queue_age,
            },
        )
        self.vlm_manager = VLMRequestManager(
            tts_queue=self.tts_queue,
            context_buffer=self.ctx_buffer,
            fast_history=self.fast_hist,
            conversation_history=self.conv_hist,
            slow_spoken_history=self.slow_spoken_hist,
            vlm_model=cfg.vlm_model,
            vlm_max_tokens=cfg.vlm_max_tokens,
            get_seek_generation=lambda: self.asr_handler.seek_generation,
            get_actions_timeline_text=self._actions_timeline_text,
            vlm_dedup_sec=cfg.vlm_dedup_sec,
            on_busy_change=self._on_vlm_busy_change,
            on_user_error=self._on_vlm_user_error,
            vlm_nitrogen_input=cfg.vlm_nitrogen_input,
        )

        self.tts_queue.set_callbacks(
            on_start=self._on_tts_subtitle,
            on_playback=self._on_tts_playback,
            on_end=self._on_tts_end,
            on_interrupt=self._on_tts_interrupt,
        )
        self.asr_handler.on_utterance = self._on_user_utterance

    def _actions_timeline_text(self, t_sec: float) -> str:
        return self.action_timeline.summary_near(t_sec)

    def _tlog(self, tag: str, text: str):
        """终端对话时间线日志：T+秒数 [标签] 内容"""
        elapsed = time.time() - self._t0 if self._t0 else 0
        print(f"  T+{elapsed:6.1f}s  [{tag}]  {text}", flush=True)

    # ── 生命周期 ──────────────────────────────────────────────────────

    async def start(self):
        """启动推理与分析循环（不再需要打开视频文件）"""
        self._t0 = time.time()
        self._loop = asyncio.get_running_loop()
        if warmup.get_status()["status"] != "ready":
            await warmup.ensure_warmup(self.cfg)
        self.tts_engine._cache.update(warmup.get_tts_cache())
        self.nitrogen.start(self.frame_buffer)

        self._running = True
        self._main_loop_task = asyncio.create_task(self._analysis_loop())
        asyncio.create_task(self._probe_nitrogen_on_start())

        await self._broadcast({"type": "status", "state": "started"})
        await self._broadcast_asr_state()
        cfg = self.cfg
        print("\n" + "─" * 60)
        print("  分析开始  T=0")
        print(f"  NitroGen={nitrogen_mode_label(cfg)}  VLM={vlm_provider(cfg)}/{cfg.vlm_model}")
        print(f"  TTS={cfg.tts_engine}  ASR={cfg.asr_engine}  VAD阈值={cfg.vad_silence_threshold}")
        print("─" * 60, flush=True)
        logger.info(
            "GameSession started (nitrogen=%s, vlm=%s/%s, fast_tts=%s)",
            nitrogen_mode_label(cfg),
            vlm_provider(cfg),
            cfg.vlm_model,
            cfg.fast_tts_enabled,
        )

    async def stop(self):
        self._running = False
        if self._main_loop_task:
            self._main_loop_task.cancel()
            self._main_loop_task = None
        self.tts_queue.clear_and_stop()
        self.nitrogen.stop()
        self.asr_handler.force_unmute()
        asyncio.create_task(self._stop_cleanup())

    async def _stop_cleanup(self):
        try:
            await asyncio.wait_for(self.vlm_manager.cancel_all(), timeout=0.3)
        except asyncio.TimeoutError:
            logger.warning("VLM cancel timed out on stop")
        elapsed = time.time() - self._t0 if self._t0 else 0
        print(f"\n  分析结束  总时长 {elapsed:.0f}s")
        print("─" * 60, flush=True)
        logger.info("GameSession stopped")

    async def _broadcast_asr_state(self, ws: WebSocket | None = None):
        state = self.asr_handler.activity_state
        msg = {"type": "asr_state", "state": state}
        if ws is not None:
            try:
                await ws.send_json(msg)
            except Exception:
                pass
            return
        await self._broadcast(msg)

    # ── 核心分析循环 ──────────────────────────────────────────────────

    async def _probe_nitrogen_on_start(self):
        """实机 fast_api 模式：启动后探测远端 NitroGen 并通知前端。"""
        from backend.nitrogen.factory import nitrogen_backend
        if nitrogen_backend(self.cfg) != "fast_api":
            return
        import os
        from backend.nitrogen.health import check_fast_api_health

        url = os.getenv("NITROGEN_FAST_API_URL", self.cfg.nitrogen_fast_api_url)
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None, lambda: check_fast_api_health(url),
        )
        await self._broadcast_nitrogen_state(result)
        if not result.get("ok"):
            logger.error("NitroGen health probe failed: %s", result.get("message"))

    async def _broadcast_nitrogen_state(self, health: dict | None = None):
        nitro = self.nitrogen
        payload = {
            "type": "nitrogen_state",
            "backend": getattr(nitro, "backend", nitrogen_mode_label(self.cfg)),
            "inference_count": getattr(nitro, "inference_count", 0),
            "error_count": getattr(nitro, "error_count", 0),
            "timeout_count": getattr(nitro, "timeout_count", 0),
            "last_error": getattr(nitro, "last_error", None),
        }
        if health is not None:
            payload.update({
                "status": "ok" if health.get("ok") else "error",
                "message": health.get("message", ""),
                "port_open": health.get("port_open"),
                "reset_ok": health.get("reset_ok"),
                "predict_ok": health.get("predict_ok"),
            })
        elif getattr(nitro, "inference_count", 0) > 0:
            payload["status"] = "ok"
        elif getattr(nitro, "last_error", None):
            payload["status"] = "error"
            payload["message"] = nitro.last_error
        else:
            payload["status"] = "waiting"
        await self._broadcast(payload)

    async def _analysis_loop(self):
        interval = 1.0 / nitrogen_analysis_fps(self.cfg)
        last_error_broadcast = 0.0

        while self._running:
            signal     = self.nitrogen.latest_signal
            video_time = self.frame_buffer.video_position

            if signal is not None:
                if not self._analysis_paused:
                    self.ctx_buffer.push_signal(video_time, signal)

                await self._broadcast({
                    "type":       "perception",
                    "intent":     signal.primary_intent,
                    "confidence": round(signal.confidence, 3),
                    "direction":  signal.move_direction,
                    "horizon":    signal.horizon_sequence,
                    "video_time": round(video_time, 2),
                    "steer":      round(signal.steer, 3),
                    "throttle":   signal.throttle,
                    "brake":      signal.brake,
                    "hint":       signal.hint_text or None,
                    "is_change":  signal.is_action_change,
                })

                if not self._analysis_paused:
                    event = self.action_filter.process(
                        signal, video_time,
                        global_min_interval=self.cfg.global_tts_min_interval,
                    )
                    if event is not None:
                        self._tlog("事件", f"{event.type.value} fast={event.trigger_fast} slow={event.trigger_slow}")
                        await self._handle_event(event)

            last_err = getattr(self.nitrogen, "last_error", None)
            err_count = getattr(self.nitrogen, "error_count", 0)
            infer_count = getattr(self.nitrogen, "inference_count", 0)
            if last_err and infer_count == 0 and err_count != last_error_broadcast:
                last_error_broadcast = err_count
                await self._broadcast_nitrogen_state()

            await asyncio.sleep(interval)

    async def _handle_event(self, event: GameEvent):
        self.ctx_buffer.push_event(event.timestamp, event)
        seek_gen = self.asr_handler.seek_generation

        if event.trigger_fast and self.cfg.fast_tts_enabled:
            text = render_fast(event)
            self.fast_hist.record(event.timestamp, text)
            if seek_gen == self.asr_handler.seek_generation:
                self._tlog("快提示", text)
                self.tts_queue.push(text, Priority.FAST_HINT)

        if event.trigger_slow:
            frame = self.frame_buffer.latest_frame
            if frame is not None:
                await self.vlm_manager.submit(
                    event, frame, utterance_seek_gen=seek_gen,
                )

    async def _trigger_first_frame_greeting(self):
        """首帧到达时，自动向慢系统发送开场白请求"""
        if self._first_frame_greeted or self._analysis_paused:
            return
        self._first_frame_greeted = True

        frame = self.frame_buffer.latest_frame
        if frame is None:
            return

        game = self.current_game or "游戏"
        from backend.nitrogen.parser import PerceptionSignal
        dummy_signal = PerceptionSignal(
            primary_intent="WAIT", confidence=0.0,
            move_direction=None, move_magnitude=0.0,
        )
        event = GameEvent(
            type=EventType.GREETING,
            timestamp=self.frame_buffer.video_position,
            perception=dummy_signal,
            trigger_fast=False,
            trigger_slow=True,
            user_text=f"我们开始玩{game}啦",
        )
        self._tlog("开场白", f"首帧自动问候: {game}")
        await self.vlm_manager.submit(
            event, frame,
            utterance_seek_gen=self.asr_handler.seek_generation,
        )

    # ── 用户语音 ──────────────────────────────────────────────────────

    def _schedule(self, coro):
        """从非 asyncio 线程安全调度协程到 GameSession 事件循环"""
        loop = self._loop
        if loop is None or not loop.is_running():
            logger.warning("GameSession loop unavailable, dropping broadcast")
            return
        asyncio.run_coroutine_threadsafe(coro, loop)

    def _on_user_utterance(self, text: str, utterance_gen: int):
        """ASR 转写完成回调（在转写线程中调用）"""
        self._tlog("用户说", text)
        logger.info("User question: %s", text)
        self._schedule(self._handle_user_utterance(text, utterance_gen))

    async def _handle_user_utterance(self, text: str, utterance_gen: int):
        if utterance_gen != self.asr_handler.seek_generation:
            logger.debug("User utterance discarded (stale after seek): %s", text)
            return

        await self._broadcast({
            "type":       "tts",
            "channel":    "user",
            "text":       text,
            "video_time": round(self.frame_buffer.video_position, 2),
        })

        from backend.nitrogen.parser import PerceptionSignal
        dummy_signal = self.nitrogen.latest_signal or PerceptionSignal(
            primary_intent="WAIT", confidence=0.0,
            move_direction=None, move_magnitude=0.0,
        )
        event = GameEvent(
            type=EventType.USER_QUESTION,
            timestamp=self.frame_buffer.video_position,
            perception=dummy_signal,
            trigger_fast=False,
            trigger_slow=True,
            user_text=text,
        )
        frame = self.frame_buffer.latest_frame
        if frame is None and self._video_frame_count > 0:
            for _ in range(10):
                await asyncio.sleep(0.05)
                frame = self.frame_buffer.latest_frame
                if frame is not None:
                    break
        if frame is None:
            logger.warning(
                "User question skipped: no video frame (frames_rx=%d). "
                "请确保视频已播放或至少有一帧画面",
                self._video_frame_count,
            )
            await self._broadcast({
                "type":  "status",
                "state": "user_question_no_frame",
                "text":  text,
                "message": "画面未就绪，无法回答。请点击播放视频后再提问。",
            })
            await self._broadcast({"type": "request_frame"})
            return

        await self.vlm_manager.submit(
            event, frame, utterance_seek_gen=utterance_gen,
        )

    # ── 视频控制 ──────────────────────────────────────────────────────

    async def on_video_ready(self, duration: float):
        """前端视频加载完成，记录时长"""
        self.frame_buffer.duration_sec = duration
        await self._broadcast({"type": "status", "state": "video_ready",
                                "duration": duration})
        logger.info("Video ready, duration=%.1fs", duration)

    async def on_seek(self, new_time: float):
        """前端拖动进度条"""
        was_analysis_paused = self._analysis_paused
        self._analysis_paused = True
        try:
            self.nitrogen.pause()
            self.tts_queue.clear_and_stop()
            self.asr_handler.reset_for_seek()
            self.asr_handler.force_unmute()
            await self.vlm_manager.cancel_all()

            self.ctx_buffer.clear()
            self.fast_hist.clear()
            self.action_filter.reset()
            self.nitrogen.clear_signal()
            self.frame_buffer.seek(new_time)

            self.nitrogen.resume()
            await self._broadcast({"type": "seek_done", "time": new_time})
        finally:
            self._analysis_paused = was_analysis_paused

    async def on_pause(self):
        """暂停视频分析；麦克风与画面帧缓冲保持可用（便于暂停时提问）。"""
        self._analysis_paused = True
        self.nitrogen.pause()
        self.tts_queue.clear_and_stop()
        await self.vlm_manager.cancel_all()

    async def on_resume(self):
        self._analysis_paused = False
        self.nitrogen.resume()
        self.asr_handler.force_unmute()

    async def on_video_ended(self):
        """视频播放结束：停止 NitroGen/快通道，保留最后一帧供语音问答"""
        self._analysis_paused = True
        self.nitrogen.pause()
        self.tts_queue.clear_and_stop()
        await self.vlm_manager.cancel_all()
        self.asr_handler.force_unmute()
        await self._broadcast({"type": "video_ended"})
        await self._broadcast({
            "type": "status",
            "state": "video_ended_can_ask",
            "message": "视频已结束，仍可语音提问（使用最后一帧画面）",
        })
        await self._broadcast_asr_state()

    async def on_clear_conversation(self):
        """清空多轮对话历史（seek 时保留，由用户主动触发）"""
        self.conv_hist.clear()
        self.slow_spoken_hist.clear()
        await self._broadcast({"type": "conversation_cleared"})

    # ── 帧与音频输入 ──────────────────────────────────────────────────

    def on_video_frame(self, jpeg_bytes: bytes, video_time: float):
        """Fix 11：前端推帧（JPEG 解码在线程池，避免阻塞 WebSocket 心跳）"""
        loop = self._loop
        if loop is None or not loop.is_running():
            return

        def _decode_and_push():
            self.frame_buffer.push(jpeg_bytes, video_time)
            self._video_frame_count += 1
            if self._video_frame_count == 1:
                logger.info(
                    "First video frame received (t=%.2fs, %d bytes)",
                    video_time, len(jpeg_bytes),
                )
                if not self._first_frame_greeted:
                    asyncio.run_coroutine_threadsafe(
                        self._trigger_first_frame_greeting(), loop
                    )

        loop.run_in_executor(None, _decode_and_push)

    def on_audio_chunk(self, pcm_bytes: bytes):
        """Fix 13：ASR 音频（非阻塞，立即返回）"""
        self._pcm_chunk_count += 1
        if self._pcm_chunk_count == 1:
            logger.info("ASR: first PCM chunk received (%d bytes)", len(pcm_bytes))
        elif self._pcm_chunk_count % 200 == 0:
            logger.debug("ASR: %d PCM chunks received", self._pcm_chunk_count)
        self.asr_handler.process_audio_chunk(pcm_bytes)

    # ── TTS 回调 ──────────────────────────────────────────────────────

    def _on_vlm_busy_change(self, busy: bool):
        self._schedule(self._broadcast({"type": "vlm_state", "busy": busy}))

    def _on_vlm_user_error(self, message: str):
        self._tlog("VLM错误", message)
        self._schedule(self._broadcast({
            "type": "status",
            "state": "vlm_error",
            "message": message,
        }))

    def _on_asr_state_change(self, state: str):
        self._schedule(self._broadcast({"type": "asr_state", "state": state}))

    def _on_asr_barge_in(self) -> bool:
        """用户说话打断 TTS，恢复收音（仅在实际播报时生效）。返回是否已处理。"""
        if not self.tts_queue.is_speaking:
            logger.debug("Barge-in ignored (TTS not playing)")
            return False
        logger.info("Barge-in: user speech interrupted TTS")
        self.tts_queue.barge_in_interrupt()
        return True

    def _on_tts_subtitle(self, text: str, channel: str, utterance_id: int):
        """字幕先出（合成中），此时不 mute 麦克风。"""
        tag = {"user_answer": "教练答", "fast": "快播报", "slow": "慢播报"}.get(channel, channel)
        self._tlog(tag, text)
        self._schedule(self._broadcast({
            "type":          "tts",
            "utterance_id":  utterance_id,
            "channel":       channel,
            "text":          text,
            "video_time":    round(self.frame_buffer.video_position, 2),
            "playing":       False,
            "synthesizing":  True,
        }))

    def _on_tts_playback(self, utterance_id: int, channel: str, text: str = ""):
        """MP3 就绪、即将播放 → 此时才 mute 麦克风。"""
        self._tlog("播报中", f"#{utterance_id} 开始播放")
        if channel == "slow" and text:
            self.slow_spoken_hist.record(text)
        self._schedule(self._broadcast({
            "type":          "tts",
            "utterance_id":  utterance_id,
            "channel":       channel,
            "playing":       True,
            "synthesizing":  False,
        }))

    def _on_tts_interrupt(self, utterance_id: int):
        self._schedule(self._broadcast({
            "type":         "tts_interrupt",
            "utterance_id": utterance_id,
        }))

    def _on_tts_end(self):
        self._schedule(self._broadcast({"type": "tts_end"}))

    def _broadcast_tts_audio(self, utterance_id: int, audio_bytes: bytes):
        """将 MP3 打包 utterance_id 后发送给主 WebSocket 客户端播放"""
        framed = frame_tts_audio(utterance_id, audio_bytes)
        self._schedule(self._broadcast_binary(framed, primary_only=True))

    # ── 广播工具 ──────────────────────────────────────────────────────

    async def _broadcast(self, msg: dict):
        dead = []
        for ws in self._ws_clients:
            try:
                await ws.send_json(msg)
            except Exception:
                dead.append(ws)
        _remove_dead_ws_clients(dead)

    async def _broadcast_binary(self, data: bytes, *, primary_only: bool = False):
        """Fix 14：向客户端发送二进制数据（TTS 音频默认仅主连接）"""
        if primary_only and _primary_ws is not None:
            targets = [_primary_ws]
        else:
            targets = list(self._ws_clients)
        dead = []
        for ws in targets:
            try:
                await ws.send_bytes(data)
            except Exception:
                dead.append(ws)
        _remove_dead_ws_clients(dead)


# ═══════════════════════════════════════════════════════════════════════
# HTTP 端点
# ═══════════════════════════════════════════════════════════════════════

@app.get("/")
async def index():
    html_path = FRONTEND_DIR / "index.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>陪玩</h1>")


@app.get("/favicon.ico")
async def favicon():
    return HTMLResponse(status_code=204)


@app.get("/api/games")
async def games_list():
    games_file = Path(__file__).parent / "games.json"
    if games_file.exists():
        return JSONResponse(json.loads(games_file.read_text(encoding="utf-8")))
    return JSONResponse([])


@app.get("/probe")
async def probe_page():
    """浏览器 E2E 链路探针页面"""
    html_path = FRONTEND_DIR / "probe.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>probe.html not found</h1>", status_code=404)


@app.get("/nitrogen/health")
async def nitrogen_health():
    """探测 NitroGen FastAPI（SSH 隧道 + /reset + /predict）。"""
    cfg = get_config()
    from backend.nitrogen.factory import nitrogen_backend
    backend = nitrogen_backend(cfg)
    if backend == "mock":
        return {"ok": True, "backend": "mock", "message": "mock 模式无需远端"}
    if backend != "fast_api":
        return {
            "ok": False,
            "backend": backend,
            "message": f"当前后端为 {backend}，仅 fast_api 支持 HTTP 探针",
        }
    import os
    from backend.nitrogen.health import check_fast_api_health
    from backend.nitrogen.ssh_tunnel import ensure_nitrogen_ssh_tunnel

    url = os.getenv("NITROGEN_FAST_API_URL", cfg.nitrogen_fast_api_url)
    tunnel_error = None
    try:
        ensure_nitrogen_ssh_tunnel(url)
    except Exception as e:
        tunnel_error = str(e)
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        None, lambda: check_fast_api_health(url),
    )
    result["backend"] = backend
    if tunnel_error:
        result["tunnel_error"] = tunnel_error
    return result


@app.get("/probe/health")
async def probe_health():
    """探针：服务端组件快照"""
    cfg = get_config()
    backend = nitrogen_mode_label(cfg)
    nitro = None
    nitrogen_health_data = None
    if _session is not None:
        nitro = {
            "mode": getattr(_session.nitrogen, "backend", backend),
            "running": _session.nitrogen._running,
            "inference_count": getattr(_session.nitrogen, "inference_count", 0),
            "timeout_count": getattr(_session.nitrogen, "timeout_count", 0),
            "error_count": getattr(_session.nitrogen, "error_count", 0),
            "last_error": getattr(_session.nitrogen, "last_error", None),
        }
    if backend == "fast_api":
        import os
        from backend.nitrogen.health import check_fast_api_health
        url = os.getenv("NITROGEN_FAST_API_URL", cfg.nitrogen_fast_api_url)
        try:
            nitrogen_health_data = check_fast_api_health(url, probe_predict=False)
        except Exception as e:
            nitrogen_health_data = {"ok": False, "message": str(e)}
    return {
        "ok": True,
        "websocket_ready": _websocket_stack_ready(),
        "nitrogen_mode": "mock" if backend == "mock" else "live",
        "nitrogen_backend": backend,
        "vlm_mode": vlm_provider(cfg),
        "vlm_model": cfg.vlm_model,
        "actions_timeline_ready": _action_timeline is not None,
        "actions_key_count": (
            len(_action_timeline.key_actions) if _action_timeline else 0
        ),
        "prepare": warmup.get_status(),
        "pcm_chunks": (
            _session._pcm_chunk_count if _session is not None else 0
        ),
        "video_frames": (
            _session._video_frame_count if _session is not None else 0
        ),
        "has_video_frame": (
            _session.frame_buffer.latest_frame is not None
            if _session is not None else False
        ),
        "asr_state": (
            _session.asr_handler.activity_state
            if _session is not None else None
        ),
        "session_running": _session is not None and _session._running,
        "ws_clients": len(_ws_clients),
        "has_primary": _primary_ws is not None,
        "nitrogen": nitro,
        "nitrogen_health": nitrogen_health_data,
    }


@app.post("/probe/tts-echo")
async def probe_tts_echo():
    """探针：向 TTS 队列注入短句，验证合成 → WS 二进制 → tts_done 链路"""
    if _session is None or not _session._running:
        return JSONResponse(
            status_code=503,
            content={"error": "分析会话未运行，请先通过探针或 /start 启动"},
        )
    text = "探针测试，链路正常。"
    # USER_ANSWER：抢占队列并打断当前播报，避免 FAST_HINT 2s 过期导致探针永远等不到 tts
    _session.tts_queue.push(text, Priority.USER_ANSWER)
    return {"status": "queued", "text": text}


@app.get("/session/status")
async def session_status():
    """查询当前分析会话是否在运行（旁观模式连接前检查）"""
    running = _session is not None and _session._running
    return {
        "running": running,
        "has_primary": _primary_ws is not None,
        "ws_clients": len(_ws_clients),
        "websocket_ready": _websocket_stack_ready(),
        "pcm_chunks": (
            _session._pcm_chunk_count if _session is not None else 0
        ),
        "video_frames": (
            _session._video_frame_count if _session is not None else 0
        ),
        "has_video_frame": (
            _session.frame_buffer.latest_frame is not None
            if _session is not None else False
        ),
        "asr_state": (
            _session.asr_handler.activity_state
            if _session is not None else None
        ),
        "vlm_mode": vlm_provider(get_config()),
        "fast_tts_enabled": get_config().fast_tts_enabled,
    }


@app.get("/prepare/status")
async def prepare_status():
    """视频选中后后台预热进度（Whisper + TTS 缓存）"""
    cfg = get_config()
    st = warmup.get_status()
    st["vlm_mode"] = vlm_provider(cfg)
    st["vlm_model"] = cfg.vlm_model
    st["vlm_key_set"] = bool(cfg.vlm_api_key or os.getenv("VLM_API_KEY"))
    return st


@app.post("/prepare")
async def prepare_resources(wait: bool = False):
    """
    选择视频后即可调用：后台加载 Whisper 与 TTS 预缓存。
    wait=true 时阻塞直到就绪（「开始分析」前调用）。
    """
    cfg = get_config()
    st = warmup.get_status()
    if st["status"] != "ready":
        if st["status"] != "loading":
            await warmup.start_background_warmup(cfg)
        if wait:
            await warmup.ensure_warmup(cfg)
    st = warmup.get_status()
    st["vlm_mode"] = vlm_provider(cfg)
    st["vlm_model"] = cfg.vlm_model
    st["vlm_key_set"] = bool(cfg.vlm_api_key or os.getenv("VLM_API_KEY"))
    return st


class FrameSampleIn(BaseModel):
    t_sec: float = Field(ge=0)
    jpeg_b64: str


class IngestBatchIn(BaseModel):
    duration_sec: float = Field(gt=0)
    sample_interval_sec: float = Field(default=2.0, gt=0)
    frames: list[FrameSampleIn]


@app.get("/actions/timeline")
async def get_actions_timeline():
    """返回当前视频的关键动作 JSON 时间线（mock 或帧扫描结果）。"""
    global _action_timeline
    if _timeline_building:
        return JSONResponse(
            status_code=202,
            content={"status": "building", "message": "动作时间线生成中"},
        )
    if _action_timeline is None:
        return JSONResponse(
            status_code=404,
            content={"error": "尚未生成动作时间线，请等待视频帧扫描完成"},
        )
    return _action_timeline.to_dict()


@app.post("/actions/ingest-batch")
async def ingest_action_frames(body: IngestBatchIn):
    """
    前端从视频抽帧后批量提交 → NitroGen 预测 → 过滤关键动作 → JSON。
    立即返回 accepted，后台构建（避免阻塞「开始分析」）。
    """
    import base64

    global _action_timeline, _timeline_building
    samples: list[tuple[float, bytes | None]] = []
    for fr in body.frames:
        jpeg = None
        if fr.jpeg_b64:
            raw = fr.jpeg_b64
            if "," in raw:
                raw = raw.split(",", 1)[1]
            try:
                jpeg = base64.b64decode(raw)
            except Exception:
                jpeg = None
        samples.append((fr.t_sec, jpeg))

    if _timeline_building:
        return {
            "status": "building",
            "frames": len(samples),
            "building": True,
        }

    if _session is not None and _session._running:
        logger.info(
            "Defer action timeline build (%d frames) while analysis session is running",
            len(samples),
        )
        return {
            "status": "deferred",
            "frames": len(samples),
            "building": False,
            "reason": "session_running",
        }

    _timeline_building = True

    def _build_sync():
        global _action_timeline, _timeline_building
        try:
            timeline = build_timeline_from_samples(
                samples,
                duration_sec=body.duration_sec,
                sample_interval_sec=body.sample_interval_sec,
            )
            _action_timeline = timeline
            if _session is not None:
                _session.action_timeline = timeline
            logger.info(
                "Action timeline ready: %d key actions",
                len(timeline.key_actions),
            )
        except Exception:
            logger.exception("Action timeline build failed")
        finally:
            _timeline_building = False

    loop = asyncio.get_running_loop()
    loop.run_in_executor(None, _build_sync)

    return {
        "status": "accepted",
        "frames": len(samples),
        "building": True,
    }


@app.post("/actions/build-mock")
async def build_mock_actions(duration_sec: float = 120.0, interval: float = 2.0):
    """无帧时按时间网格生成 mock 时间线（调试/探针用）。"""
    global _action_timeline
    _action_timeline = build_mock_timeline(duration_sec, interval)
    if _session is not None:
        _session.action_timeline = _action_timeline
    return _action_timeline.to_dict()


@app.post("/start")
async def start_session():
    """
    Fix 11：不再需要传视频路径，前端直接推帧。
    启动分析会话后，等待前端通过 WebSocket 发送视频帧。
    """
    global _session
    if _session is not None and _session._running:
        return JSONResponse(
            status_code=409,
            content={
                "status": "already_running",
                "error": "分析会话已在运行，请使用旁观模式或先停止当前会话",
            },
        )
    if _session:
        await _session.stop()
    _session = GameSession()
    global _pcm_drop_logged
    _pcm_drop_logged = False
    await _session.start()
    cfg = get_config()
    backend = nitrogen_mode_label(cfg)
    vlm_mode = vlm_provider(cfg)
    nitrogen_health_data = None
    if backend == "fast_api":
        import os
        from backend.nitrogen.health import check_fast_api_health
        url = os.getenv("NITROGEN_FAST_API_URL", cfg.nitrogen_fast_api_url)
        loop = asyncio.get_running_loop()
        nitrogen_health_data = await loop.run_in_executor(
            None, lambda: check_fast_api_health(url, probe_predict=False),
        )
    return {
        "status": "ok",
        "nitrogen_mode": "mock" if backend == "mock" else "live",
        "nitrogen_backend": backend,
        "nitrogen_health": nitrogen_health_data,
        "vlm_mode": vlm_mode,
        "vlm_model": cfg.vlm_model,
        "prepare": warmup.get_status(),
        "asr_state": _session.asr_handler.activity_state if _session else "listening",
    }


@app.post("/stop")
async def stop_session():
    global _session
    if _session:
        await _session.stop()
        _session = None
    return {"status": "ok"}


# ═══════════════════════════════════════════════════════════════════════
# WebSocket 端点
# ═══════════════════════════════════════════════════════════════════════

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """
    二进制协议（客户端 → 服务端）：
      byte[0]=0x01  PCM 音频（ASR）
      byte[0]=0x02  视频帧（NitroGen）byte[1:9]=float64 LE 时间，byte[9:]=JPEG

    二进制协议（服务端 → 客户端）：
      byte[0]=0x03  TTS 音频（byte[1:5]=uint32 LE utterance_id，byte[5:]=MP3）

    JSON（双向）：
      客户端发：register / seek / playback / video_ready / tts_done / clear_conversation
      服务端发：session_role / primary_changed / tts / tts_end / ...
    """
    global _primary_ws
    await ws.accept()
    _ws_clients.append(ws)
    logger.info("WebSocket connected (total: %d)", len(_ws_clients))

    try:
        while True:
            try:
                msg = await ws.receive()
            except WebSocketDisconnect:
                break
            except RuntimeError as exc:
                if "disconnect" in str(exc).lower():
                    break
                raise

            # ── 二进制消息 ────────────────────────────────────────────
            if "bytes" in msg and msg["bytes"]:
                data = msg["bytes"]
                if len(data) < 1:
                    continue

                msg_type = data[0]

                if msg_type == 0x01:
                    # PCM 音频 → ASR（仅主客户端）
                    if ws is not _primary_ws:
                        continue
                    if _session:
                        _session.on_audio_chunk(data[1:])
                    else:
                        global _pcm_drop_logged
                        if not _pcm_drop_logged:
                            _pcm_drop_logged = True
                            logger.warning(
                                "PCM received but no active session "
                                "(请先 POST /start 再推麦克风)"
                            )

                elif msg_type == 0x02:
                    # 视频帧 → NitroGen（仅主客户端）
                    if ws is not _primary_ws:
                        continue
                    if len(data) >= 9 and _session:
                        video_time = struct.unpack_from("<d", data, 1)[0]
                        jpeg_bytes = data[9:]
                        _session.on_video_frame(jpeg_bytes, video_time)

            # ── JSON 消息 ─────────────────────────────────────────────
            elif "text" in msg and msg["text"]:
                import json
                try:
                    data = json.loads(msg["text"])
                    mtype = data.get("type")

                    if mtype == "register":
                        await _handle_register(ws, data)
                        continue

                    if mtype == "request_asr_state" and _session:
                        await _session._broadcast_asr_state(ws)
                        continue

                    if ws is not _primary_ws and mtype not in (None,):
                        logger.debug("Ignored %s from non-primary client", mtype)
                        continue

                    if mtype == "video_ready" and _session:
                        await _session.on_video_ready(float(data.get("duration", 0)))

                    elif mtype == "seek" and _session:
                        await _session.on_seek(float(data["time"]))

                    elif mtype == "playback" and _session:
                        if data.get("action") == "pause":
                            await _session.on_pause()
                        elif data.get("action") == "resume":
                            await _session.on_resume()

                    elif mtype == "video_ended" and _session:
                        await _session.on_video_ended()

                    elif mtype == "clear_conversation" and _session:
                        await _session.on_clear_conversation()

                    elif mtype == "tts_done" and _session:
                        uid = int(data.get("utterance_id", -1))
                        if uid >= 0:
                            _session.tts_queue.on_client_tts_done(uid)

                    elif mtype in ("set_voice_fast", "set_voice_slow") and _session:
                        speaker = data.get("speaker", "")
                        is_fast = mtype == "set_voice_fast"
                        attr = "_volc_speaker_fast" if is_fast else "_volc_speaker_slow"
                        if speaker:
                            old_speaker = getattr(_session.tts_engine, attr)
                            setattr(_session.tts_engine, attr, speaker)
                            _session.tts_engine._cache.clear()
                            label = "快系统" if is_fast else "慢系统"
                            logger.info("%s voice changed to: %s", label, speaker)

                            async def _validate_voice(old_spk, new_spk, _attr, _label):
                                loop = asyncio.get_running_loop()
                                try:
                                    audio = await loop.run_in_executor(
                                        None,
                                        _session.tts_engine._synthesize_full_volc,
                                        "测试",
                                        new_spk,
                                    )
                                    if not audio:
                                        raise RuntimeError("empty audio")
                                except Exception as e:
                                    logger.warning(
                                        "Voice %s validation failed, reverting to %s: %s",
                                        new_spk, old_spk, e,
                                    )
                                    setattr(_session.tts_engine, _attr, old_spk)
                                    _session.tts_engine._cache.clear()
                                    await _session._broadcast({
                                        "type": "voice_error",
                                        "speaker": new_spk,
                                        "message": f"{_label}音色 {new_spk} 不可用，已恢复原音色",
                                    })

                            asyncio.create_task(_validate_voice(old_speaker, speaker, attr, label))

                    elif mtype == "set_game" and _session:
                        game = data.get("game", "")
                        if game:
                            _session.current_game = game
                            logger.info("Game changed to: %s", game)

                    elif mtype == "set_asr" and _session:
                        enabled = data.get("enabled", False)
                        if enabled:
                            _session.asr_handler.force_unmute()
                            logger.info("ASR enabled by user")
                        else:
                            _session.asr_handler.mute()
                            logger.info("ASR disabled by user")

                except Exception as e:
                    logger.error("WS JSON error: %s", e)

    finally:
        was_primary = (_primary_ws is ws)
        if ws in _ws_clients:
            _ws_clients.remove(ws)
        _ws_roles.pop(ws, None)
        if _primary_ws is ws:
            _primary_ws = None
            _reassign_primary_from_players()
        close_code = getattr(ws, "close_code", None)
        logger.info(
            "WebSocket disconnected (total: %d, primary_lost=%s, code=%s)",
            len(_ws_clients), was_primary, close_code,
        )

        if was_primary and _primary_ws is not None:
            await _send_session_role(_primary_ws)
        if was_primary and _primary_ws is None and _session is not None:
            try:
                await _session.on_pause()
            except Exception as e:
                logger.error("Pause after primary lost: %s", e)
        if was_primary and _session is not None:
            await _session._broadcast({"type": "primary_changed"})


if __name__ == "__main__":
    uvicorn.run("backend.main:app", host="0.0.0.0", port=8000, reload=False)
