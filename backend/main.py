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
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

from backend.config import get_config
from backend.video.frame_buffer import FrameBuffer          # Fix 11
from backend.nitrogen.client import NitroGenClient
from backend.fast.action_filter import ActionFilter
from backend.fast.templates import render_fast
from backend.fast.event import EventType, GameEvent
from backend.slow.context_buffer import ContextBuffer, ConversationHistory, FastHistory
from backend.slow.trigger import VLMRequestManager
from backend.tts.engine import TTSEngine
from backend.tts.protocol import frame_tts_audio
from backend.tts.queue import TTSQueue, Priority
from backend.asr.handler import ASRHandler

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

        self.nitrogen = NitroGenClient(
            server_addr=os.getenv("NITROGEN_SERVER", cfg.nitrogen_server)
        )
        self.action_filter = ActionFilter(
            confidence_threshold=cfg.fast_trigger_confidence,
            sustained_danger_sec=cfg.sustained_danger_sec,
            cooldowns=cfg.cooldowns,
        )

        self.ctx_buffer = ContextBuffer(window_sec=cfg.context_window_sec)
        self.conv_hist  = ConversationHistory()
        self.fast_hist  = FastHistory()

        self.tts_engine  = TTSEngine(voice=cfg.tts_voice, rate=cfg.tts_rate)
        self.asr_handler = ASRHandler(
            model_size=cfg.whisper_model,
            language=cfg.whisper_language,
            vad_silence_threshold=cfg.vad_silence_threshold,
            vad_speech_min_sec=cfg.vad_speech_min_sec,
            vad_silence_end_sec=cfg.vad_silence_end_sec,
            tts_mute_tail_sec=cfg.tts_mute_tail_sec,
        )
        self.asr_handler.on_state_change = self._on_asr_state_change
        self.tts_queue = TTSQueue(
            tts_engine=self.tts_engine,
            asr_handler=self.asr_handler,
            inter_gap=cfg.tts_inter_utterance_gap,
            fallback_margin=cfg.tts_done_fallback_margin,
            broadcast_audio=self._broadcast_tts_audio,   # Fix 14
        )
        self.vlm_manager = VLMRequestManager(
            tts_queue=self.tts_queue,
            context_buffer=self.ctx_buffer,
            fast_history=self.fast_hist,
            conversation_history=self.conv_hist,
            vlm_model=cfg.vlm_model,
            vlm_max_tokens=cfg.vlm_max_tokens,
        )

        self.tts_queue.set_callbacks(
            on_start=self._on_tts_start,
            on_end=self._on_tts_end,
            on_interrupt=self._on_tts_interrupt,
        )
        self.asr_handler.on_utterance = self._on_user_utterance

        self._main_loop_task: Optional[asyncio.Task] = None
        self._running = False

    # ── 生命周期 ──────────────────────────────────────────────────────

    async def start(self):
        """启动推理与分析循环（不再需要打开视频文件）"""
        self.nitrogen.start(self.frame_buffer)   # Fix 11：传 FrameBuffer
        self.tts_engine.preload()

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
        self.nitrogen.stop()
        self.asr_handler.stop()
        logger.info("GameSession stopped")

    # ── 核心分析循环 ──────────────────────────────────────────────────

    async def _analysis_loop(self):
        interval = 1.0 / self.cfg.nitrogen_target_fps

        while self._running:
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

        if event.trigger_fast:
            text = render_fast(event)
            self.fast_hist.record(event.timestamp, text)
            self.tts_queue.push(text, Priority.FAST_HINT)

        if event.trigger_slow:
            frame = self.frame_buffer.latest_frame
            if frame is not None:
                await self.vlm_manager.submit(event, frame)

    # ── 用户语音 ──────────────────────────────────────────────────────

    def _on_user_utterance(self, text: str):
        """ASR 转写完成回调（在转写线程中调用）"""
        logger.info("User question: %s", text)

        loop = asyncio.get_event_loop()
        asyncio.run_coroutine_threadsafe(
            self._broadcast({
                "type":       "tts",
                "channel":    "user",
                "text":       text,
                "video_time": round(self.frame_buffer.video_position, 2),
            }),
            loop,
        )

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
        if frame is not None:
            asyncio.run_coroutine_threadsafe(
                self.vlm_manager.submit(event, frame),
                loop,
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
        self.nitrogen.pause()
        self.tts_queue.clear_and_stop()
        self.asr_handler.force_unmute()
        await self.vlm_manager.cancel_all()

        self.ctx_buffer.clear()
        self.fast_hist.clear()
        self.action_filter.reset()
        self.frame_buffer.seek(new_time)   # Fix 11：清空旧帧

        self.nitrogen.resume()
        await self._broadcast({"type": "seek_done", "time": new_time})

    async def on_pause(self):
        self.frame_buffer.pause()
        self.nitrogen.pause()

    async def on_resume(self):
        self.frame_buffer.resume()
        self.nitrogen.resume()

    # ── 帧与音频输入 ──────────────────────────────────────────────────

    def on_video_frame(self, jpeg_bytes: bytes, video_time: float):
        """Fix 11：前端推帧（在 WebSocket 协程中调用）"""
        self.frame_buffer.push(jpeg_bytes, video_time)

    def on_audio_chunk(self, pcm_bytes: bytes):
        """Fix 13：ASR 音频（非阻塞，立即返回）"""
        self.asr_handler.process_audio_chunk(pcm_bytes)

    # ── TTS 回调 ──────────────────────────────────────────────────────

    def _on_asr_state_change(self, state: str):
        asyncio.run_coroutine_threadsafe(
            self._broadcast({"type": "asr_state", "state": state}),
            asyncio.get_event_loop(),
        )

    def _on_tts_start(self, text: str, channel: str, utterance_id: int):
        """TTS 开始播报 → 广播 JSON 事件（供前端更新 UI，在 MP3 之前）"""
        asyncio.run_coroutine_threadsafe(
            self._broadcast({
                "type":          "tts",
                "utterance_id":  utterance_id,
                "channel":       channel,
                "text":          text,
                "video_time":    round(self.frame_buffer.video_position, 2),
                "playing":       True,
            }),
            asyncio.get_event_loop(),
        )

    def _on_tts_interrupt(self, utterance_id: int):
        asyncio.run_coroutine_threadsafe(
            self._broadcast({
                "type":         "tts_interrupt",
                "utterance_id": utterance_id,
            }),
            asyncio.get_event_loop(),
        )

    def _on_tts_end(self):
        asyncio.run_coroutine_threadsafe(
            self._broadcast({"type": "tts_end"}),
            asyncio.get_event_loop(),
        )

    def _broadcast_tts_audio(self, utterance_id: int, audio_bytes: bytes):
        """将 MP3 打包 utterance_id 后广播给所有 WebSocket 客户端"""
        framed = frame_tts_audio(utterance_id, audio_bytes)
        asyncio.run_coroutine_threadsafe(
            self._broadcast_binary(framed),
            asyncio.get_event_loop(),
        )

    # ── 广播工具 ──────────────────────────────────────────────────────

    async def _broadcast(self, msg: dict):
        dead = []
        for ws in self._ws_clients:
            try:
                await ws.send_json(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            if ws in _ws_clients:
                _ws_clients.remove(ws)

    async def _broadcast_binary(self, data: bytes):
        """Fix 14：向所有客户端发送二进制数据（TTS 音频）"""
        dead = []
        for ws in self._ws_clients:
            try:
                await ws.send_bytes(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            if ws in _ws_clients:
                _ws_clients.remove(ws)


# ═══════════════════════════════════════════════════════════════════════
# HTTP 端点
# ═══════════════════════════════════════════════════════════════════════

@app.get("/")
async def index():
    html_path = FRONTEND_DIR / "index.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>NitroGen Game Coach</h1>")


@app.post("/start")
async def start_session():
    """
    Fix 11：不再需要传视频路径，前端直接推帧。
    启动分析会话后，等待前端通过 WebSocket 发送视频帧。
    """
    global _session
    if _session:
        await _session.stop()
    _session = GameSession()
    await _session.start()
    return {"status": "ok"}


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
      客户端发：seek / playback / video_ready / tts_done
      服务端发：tts / tts_end / perception / status / seek_done / video_ended
    """
    await ws.accept()
    _ws_clients.append(ws)
    logger.info("WebSocket connected (total: %d)", len(_ws_clients))

    try:
        while True:
            msg = await ws.receive()

            # ── 二进制消息 ────────────────────────────────────────────
            if "bytes" in msg and msg["bytes"]:
                data = msg["bytes"]
                if len(data) < 1:
                    continue

                msg_type = data[0]

                if msg_type == 0x01:
                    # PCM 音频 → ASR
                    if _session:
                        _session.on_audio_chunk(data[1:])

                elif msg_type == 0x02:
                    # 视频帧 → NitroGen
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
                        await _session._broadcast({"type": "video_ended"})

                    elif mtype == "tts_done" and _session:
                        uid = int(data.get("utterance_id", -1))
                        if uid >= 0:
                            _session.tts_queue.on_client_tts_done(uid)

                except Exception as e:
                    logger.error("WS JSON error: %s", e)

    except WebSocketDisconnect:
        if ws in _ws_clients:
            _ws_clients.remove(ws)
        logger.info("WebSocket disconnected (total: %d)", len(_ws_clients))


if __name__ == "__main__":
    uvicorn.run("backend.main:app", host="0.0.0.0", port=8000, reload=False)
