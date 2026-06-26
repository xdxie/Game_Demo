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
from pathlib import Path
from typing import Optional

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


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
from backend.nitrogen.factory import create_nitrogen_client, nitrogen_mock_enabled
from backend.fast.action_filter import ActionFilter
from backend.fast.templates import render_fast
from backend.fast.event import EventType, GameEvent
from backend.slow.context_buffer import ContextBuffer, ConversationHistory, FastHistory
from backend.slow.trigger import VLMRequestManager
from backend.tts.engine import TTSEngine
from backend.tts.protocol import frame_tts_audio
from backend.tts.queue import TTSQueue, Priority
from backend.asr.handler import ASRHandler
from backend import warmup
from backend.slow.vlm_factory import vlm_mock_enabled

import os

app = FastAPI(title="NitroGen Game Coach")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

_session: Optional["GameSession"] = None
_ws_clients: list[WebSocket] = []
_ws_roles: dict[WebSocket, str] = {}   # "player" | "observer"
_primary_ws: Optional[WebSocket] = None


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

        # Fix 11：FrameBuffer 接收前端推帧
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

        self.tts_engine  = TTSEngine(
            voice=cfg.tts_voice,
            rate=cfg.tts_rate,
            synthesis_timeout=cfg.tts_synthesis_timeout_sec,
        )
        tts_cache = warmup.get_tts_cache()
        if tts_cache:
            self.tts_engine._cache.update(tts_cache)

        whisper = warmup.get_whisper_model(cfg)
        self.asr_handler = ASRHandler(
            model_size=cfg.whisper_model,
            language=cfg.whisper_language,
            vad_silence_threshold=cfg.vad_silence_threshold,
            vad_speech_min_sec=cfg.vad_speech_min_sec,
            vad_silence_end_sec=cfg.vad_silence_end_sec,
            tts_mute_tail_sec=cfg.tts_mute_tail_sec,
            whisper_model=whisper,
        )
        self.asr_handler.on_state_change = self._on_asr_state_change
        self.tts_queue = TTSQueue(
            tts_engine=self.tts_engine,
            asr_handler=self.asr_handler,
            inter_gap=cfg.tts_inter_utterance_gap,
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
            vlm_model=cfg.vlm_model,
            vlm_max_tokens=cfg.vlm_max_tokens,
            get_seek_generation=lambda: self.asr_handler.seek_generation,
            vlm_dedup_sec=cfg.vlm_dedup_sec,
            on_busy_change=self._on_vlm_busy_change,
        )

        self.tts_queue.set_callbacks(
            on_start=self._on_tts_start,
            on_end=self._on_tts_end,
            on_interrupt=self._on_tts_interrupt,
        )
        self.asr_handler.on_utterance = self._on_user_utterance

        self._main_loop_task: Optional[asyncio.Task] = None
        self._running = False
        self._analysis_paused = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # ── 生命周期 ──────────────────────────────────────────────────────

    async def start(self):
        """启动推理与分析循环（不再需要打开视频文件）"""
        self._loop = asyncio.get_running_loop()
        await warmup.ensure_warmup(self.cfg)
        self.tts_engine._cache.update(warmup.get_tts_cache())
        self.nitrogen.start(self.frame_buffer)   # Fix 11：传 FrameBuffer
        if not self.tts_engine._cache:
            await self.tts_engine.preload_async()

        self._running = True
        self._main_loop_task = asyncio.create_task(self._analysis_loop())

        await self._broadcast({"type": "status", "state": "started"})
        logger.info("GameSession started")

    async def stop(self):
        self._running = False
        if self._main_loop_task:
            self._main_loop_task.cancel()
        await self.vlm_manager.cancel_all()
        self.tts_queue.clear_and_stop()
        self.asr_handler.force_unmute()
        self.nitrogen.stop()
        self.asr_handler.stop()
        logger.info("GameSession stopped")

    # ── 核心分析循环 ──────────────────────────────────────────────────

    async def _analysis_loop(self):
        interval = 1.0 / self.cfg.nitrogen_target_fps

        while self._running:
            if not self._analysis_paused:
                signal     = self.nitrogen.latest_signal
                video_time = self.frame_buffer.video_position

                if signal is not None:
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
                    })

                    event = self.action_filter.process(
                        signal, video_time,
                        global_min_interval=self.cfg.global_tts_min_interval,
                    )
                    if event is not None:
                        await self._handle_event(event)

            await asyncio.sleep(interval)

    async def _handle_event(self, event: GameEvent):
        self.ctx_buffer.push_event(event.timestamp, event)
        seek_gen = self.asr_handler.seek_generation

        if event.trigger_fast:
            text = render_fast(event)
            self.fast_hist.record(event.timestamp, text)
            if seek_gen == self.asr_handler.seek_generation:
                self.tts_queue.push(text, Priority.FAST_HINT)

        if event.trigger_slow:
            frame = self.frame_buffer.latest_frame
            if frame is not None:
                await self.vlm_manager.submit(
                    event, frame, utterance_seek_gen=seek_gen,
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
        if frame is None:
            logger.warning("User question skipped: no video frame available")
            await self._broadcast({
                "type":  "status",
                "state": "user_question_no_frame",
                "text":  text,
            })
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
        self._analysis_paused = True
        self.frame_buffer.pause()
        self.nitrogen.pause()
        self.tts_queue.clear_and_stop()
        await self.vlm_manager.cancel_all()
        self.asr_handler.mute()

    async def on_resume(self):
        self._analysis_paused = False
        self.frame_buffer.resume()
        self.nitrogen.resume()
        self.asr_handler.force_unmute()

    async def on_video_ended(self):
        """视频播放结束：暂停分析并停止播报"""
        self._analysis_paused = True
        self.frame_buffer.pause()
        self.nitrogen.pause()
        self.tts_queue.clear_and_stop()
        await self.vlm_manager.cancel_all()
        self.asr_handler.force_unmute()
        await self._broadcast({"type": "video_ended"})

    async def on_clear_conversation(self):
        """清空多轮对话历史（seek 时保留，由用户主动触发）"""
        self.conv_hist.clear()
        await self._broadcast({"type": "conversation_cleared"})

    # ── 帧与音频输入 ──────────────────────────────────────────────────

    def on_video_frame(self, jpeg_bytes: bytes, video_time: float):
        """Fix 11：前端推帧（在 WebSocket 协程中调用）"""
        self.frame_buffer.push(jpeg_bytes, video_time)
        notify = getattr(self.nitrogen, "on_frame_pushed", None)
        if callable(notify):
            notify()

    def on_audio_chunk(self, pcm_bytes: bytes):
        """Fix 13：ASR 音频（非阻塞，立即返回）"""
        self.asr_handler.process_audio_chunk(pcm_bytes)

    # ── TTS 回调 ──────────────────────────────────────────────────────

    def _on_vlm_busy_change(self, busy: bool):
        self._schedule(self._broadcast({"type": "vlm_state", "busy": busy}))

    def _on_asr_state_change(self, state: str):
        self._schedule(self._broadcast({"type": "asr_state", "state": state}))

    def _on_tts_start(self, text: str, channel: str, utterance_id: int):
        """TTS 开始播报 → 广播 JSON 事件（供前端更新 UI，在 MP3 之前）"""
        self._schedule(self._broadcast({
            "type":          "tts",
            "utterance_id":  utterance_id,
            "channel":       channel,
            "text":          text,
            "video_time":    round(self.frame_buffer.video_position, 2),
            "playing":       True,
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
    return HTMLResponse("<h1>NitroGen Game Coach</h1>")


@app.get("/probe")
async def probe_page():
    """浏览器 E2E 链路探针页面"""
    html_path = FRONTEND_DIR / "probe.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>probe.html not found</h1>", status_code=404)


@app.get("/probe/health")
async def probe_health():
    """探针：服务端组件快照"""
    nitro = None
    if _session is not None:
        nitro = {
            "mode": "mock" if getattr(_session.nitrogen, "is_mock", False) else "live",
            "running": _session.nitrogen._running,
            "inference_count": _session.nitrogen.inference_count,
            "timeout_count": _session.nitrogen.timeout_count,
        }
    cfg = get_config()
    return {
        "ok": True,
        "websocket_ready": _websocket_stack_ready(),
        "nitrogen_mode": "mock" if nitrogen_mock_enabled(cfg) else "live",
        "vlm_mode": "mock" if vlm_mock_enabled(cfg) else "live",
        "prepare": warmup.get_status(),
        "session_running": _session is not None and _session._running,
        "ws_clients": len(_ws_clients),
        "has_primary": _primary_ws is not None,
        "nitrogen": nitro,
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
    }


@app.get("/prepare/status")
async def prepare_status():
    """视频选中后后台预热进度（Whisper + TTS 缓存）"""
    cfg = get_config()
    st = warmup.get_status()
    st["vlm_mode"] = "mock" if vlm_mock_enabled(cfg) else "live"
    return st


@app.post("/prepare")
async def prepare_resources():
    """
    选择视频后即可调用：后台加载 Whisper 与 TTS 预缓存。
    VLM 不在此常驻加载——仅在事件触发时短时运行（mock 或 Claude）。
    """
    cfg = get_config()
    st = warmup.get_status()
    if st["status"] == "ready":
        return st
    if st["status"] != "loading":
        await warmup.start_background_warmup(cfg)
    return warmup.get_status()


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
    await _session.start()
    cfg = get_config()
    mode = "mock" if getattr(_session.nitrogen, "is_mock", False) else "live"
    return {"status": "ok", "nitrogen_mode": mode}


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
        logger.info("WebSocket disconnected (total: %d)", len(_ws_clients))

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
