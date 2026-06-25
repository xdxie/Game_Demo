"""
主入口：FastAPI + WebSocket 服务。
负责启动所有模块、协调事件流、向前端广播状态。

快慢系统主循环在这里接合：
  VideoFramePipe → NitroGenClient → ActionFilter
    → FastPath  → TTSQueue
    → SlowPath  → VLMRequestManager → TTSQueue
  ASRHandler → USER_QUESTION → VLMRequestManager → TTSQueue
  TTSQueue → WebSocket broadcast
"""

from __future__ import annotations
import asyncio
import logging
import os
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

# ── 后端模块导入 ──────────────────────────────────────────────────────
from backend.config import get_config
from backend.video.frame_pipe import VideoFramePipe
from backend.nitrogen.client import NitroGenClient
from backend.fast.action_filter import ActionFilter
from backend.fast.templates import render_fast
from backend.fast.event import EventType, GameEvent
from backend.slow.context_buffer import ContextBuffer, ConversationHistory, FastHistory
from backend.slow.trigger import VLMRequestManager
from backend.tts.engine import TTSEngine
from backend.tts.queue import TTSQueue, Priority
from backend.asr.handler import ASRHandler

# ── FastAPI App ───────────────────────────────────────────────────────
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

# ── 全局会话（单用户 demo）────────────────────────────────────────────
_session: Optional["GameSession"] = None
_ws_clients: list[WebSocket] = []


# ═══════════════════════════════════════════════════════════════════════
# GameSession：封装一次完整的视频分析会话
# ═══════════════════════════════════════════════════════════════════════

class GameSession:
    def __init__(self, video_path: str):
        cfg = get_config()

        self.cfg        = cfg
        self.video_path = video_path
        self._ws_clients = _ws_clients  # 共享连接列表

        # ── 模块初始化 ────────────────────────────────────────────────
        self.frame_pipe = VideoFramePipe(video_path, target_fps=cfg.nitrogen_target_fps)
        self.nitrogen   = NitroGenClient(
            server_addr=os.getenv("NITROGEN_SERVER", cfg.nitrogen_server)
        )
        self.action_filter = ActionFilter(
            confidence_threshold=cfg.fast_trigger_confidence,
            sustained_danger_sec=cfg.sustained_danger_sec,
            cooldowns=cfg.cooldowns,
        )

        self.ctx_buffer  = ContextBuffer(window_sec=cfg.context_window_sec)
        self.conv_hist   = ConversationHistory()
        self.fast_hist   = FastHistory()

        self.tts_engine  = TTSEngine(voice=cfg.tts_voice, rate=cfg.tts_rate)
        self.asr_handler = ASRHandler(
            model_size=cfg.whisper_model,
            language=cfg.whisper_language,
        )
        self.tts_queue = TTSQueue(
            tts_engine=self.tts_engine,
            asr_handler=self.asr_handler,
            inter_gap=cfg.tts_inter_utterance_gap,
        )
        self.vlm_manager = VLMRequestManager(
            tts_queue=self.tts_queue,
            context_buffer=self.ctx_buffer,
            fast_history=self.fast_hist,
            conversation_history=self.conv_hist,
            vlm_model=cfg.vlm_model,
            vlm_max_tokens=cfg.vlm_max_tokens,
        )

        # ── TTS 播报回调 → WebSocket 广播 ─────────────────────────────
        self.tts_queue.set_callbacks(
            on_start=self._on_tts_start,
            on_end=self._on_tts_end,
        )

        # ── ASR 识别完成回调 ──────────────────────────────────────────
        self.asr_handler.on_utterance = self._on_user_utterance

        # ── 主循环任务 ────────────────────────────────────────────────
        self._main_loop_task: Optional[asyncio.Task] = None
        self._running = False

    # ── 生命周期 ──────────────────────────────────────────────────────

    async def start(self):
        """打开视频、启动推理、启动主分析循环"""
        self.frame_pipe.open()
        self.nitrogen.start(self.frame_pipe)
        self.frame_pipe.start(on_end=self._on_video_end)
        self.tts_engine.preload()

        self._running = True
        self._main_loop_task = asyncio.create_task(self._analysis_loop())

        await self._broadcast({"type": "status", "state": "started",
                                "duration": self.frame_pipe.duration_sec})
        logger.info("GameSession started: %s", self.video_path)

    async def stop(self):
        """停止所有模块"""
        self._running = False
        if self._main_loop_task:
            self._main_loop_task.cancel()
        await self.vlm_manager.cancel_all()
        self.tts_queue.clear_and_stop()
        self.frame_pipe.stop()
        self.nitrogen.stop()
        logger.info("GameSession stopped")

    # ── 核心分析循环 ──────────────────────────────────────────────────

    async def _analysis_loop(self):
        """
        每隔 target_fps^-1 秒读一次最新感知信号，送入过滤器，
        处理触发的 GameEvent。
        """
        interval = 1.0 / self.cfg.nitrogen_target_fps

        while self._running:
            signal = self.nitrogen.latest_signal
            video_time = self.frame_pipe.video_position

            if signal is not None:
                # 更新上下文
                self.ctx_buffer.push_signal(video_time, signal)

                # 广播感知信号（调试面板）
                await self._broadcast({
                    "type":       "perception",
                    "intent":     signal.primary_intent,
                    "confidence": round(signal.confidence, 3),
                    "direction":  signal.move_direction,
                    "horizon":    signal.horizon_sequence,
                    "video_time": round(video_time, 2),
                })

                # 动作过滤
                event = self.action_filter.process(
                    signal, video_time,
                    global_min_interval=self.cfg.global_tts_min_interval,
                )

                if event is not None:
                    await self._handle_event(event)

            await asyncio.sleep(interval)

    async def _handle_event(self, event: GameEvent):
        """分发 GameEvent 到快通道和/或慢通道"""
        self.ctx_buffer.push_event(event.timestamp, event)

        # ── 快通道 ──────────────────────────────────────────────────
        if event.trigger_fast:
            text = render_fast(event)
            self.fast_hist.record(event.timestamp, text)
            self.tts_queue.push(text, Priority.FAST_HINT)

        # ── 慢通道 ──────────────────────────────────────────────────
        if event.trigger_slow:
            frame = self.frame_pipe.latest_frame
            if frame is not None:
                await self.vlm_manager.submit(event, frame)

    # ── 用户语音 ──────────────────────────────────────────────────────

    def _on_user_utterance(self, text: str):
        """ASR 识别完成，触发 USER_QUESTION 事件（在 ASR 线程中调用）"""
        logger.info("User question: %s", text)

        # 广播用户提问到前端
        asyncio.run_coroutine_threadsafe(
            self._broadcast({
                "type": "tts",
                "channel": "user",
                "text": text,
                "video_time": round(self.frame_pipe.video_position, 2),
            }),
            asyncio.get_event_loop(),
        )

        from backend.nitrogen.parser import PerceptionSignal
        dummy_signal = self.nitrogen.latest_signal or PerceptionSignal(
            primary_intent="WAIT", confidence=0.0,
            move_direction=None, move_magnitude=0.0,
        )

        event = GameEvent(
            type=EventType.USER_QUESTION,
            timestamp=self.frame_pipe.video_position,
            perception=dummy_signal,
            trigger_fast=False,
            trigger_slow=True,
            user_text=text,
        )

        frame = self.frame_pipe.latest_frame
        if frame is not None:
            asyncio.run_coroutine_threadsafe(
                self.vlm_manager.submit(event, frame),
                asyncio.get_event_loop(),
            )

    # ── 视频控制 ──────────────────────────────────────────────────────

    async def on_seek(self, new_time: float):
        """前端拖动进度条"""
        self.nitrogen.pause()
        self.tts_queue.clear_and_stop()
        self.asr_handler.force_unmute()
        await self.vlm_manager.cancel_all()

        self.ctx_buffer.clear()
        self.fast_hist.clear()
        self.action_filter.reset()
        # conv_hist 保留

        self.frame_pipe.seek(new_time)
        self.nitrogen.resume()

        await self._broadcast({"type": "seek_done", "time": new_time})

    async def on_pause(self):
        self.frame_pipe.pause()
        self.nitrogen.pause()

    async def on_resume(self):
        self.frame_pipe.resume()
        self.nitrogen.resume()

    # ── 音频输入 ──────────────────────────────────────────────────────

    def on_audio_chunk(self, audio_bytes: bytes):
        """前端麦克风音频块（WebSocket binary frame）"""
        self.asr_handler.process_audio_chunk(audio_bytes)

    # ── 内部回调 ──────────────────────────────────────────────────────

    def _on_tts_start(self, text: str, channel: str):
        asyncio.run_coroutine_threadsafe(
            self._broadcast({
                "type":    "tts",
                "channel": channel,
                "text":    text,
                "video_time": round(self.frame_pipe.video_position, 2),
                "playing": True,
            }),
            asyncio.get_event_loop(),
        )

    def _on_tts_end(self):
        asyncio.run_coroutine_threadsafe(
            self._broadcast({"type": "tts_end"}),
            asyncio.get_event_loop(),
        )

    def _on_video_end(self):
        asyncio.run_coroutine_threadsafe(
            self._broadcast({"type": "video_ended"}),
            asyncio.get_event_loop(),
        )

    # ── WebSocket 广播 ────────────────────────────────────────────────

    async def _broadcast(self, msg: dict):
        import json
        dead = []
        for ws in self._ws_clients:
            try:
                await ws.send_json(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            _ws_clients.remove(ws)


# ═══════════════════════════════════════════════════════════════════════
# HTTP 端点
# ═══════════════════════════════════════════════════════════════════════

@app.get("/")
async def index():
    html_path = FRONTEND_DIR / "index.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>NitroGen Game Coach</h1><p>Frontend not found.</p>")


@app.post("/start")
async def start_session(body: dict):
    """
    启动分析会话。
    body: {"video_path": "/path/to/video.mp4"}
    """
    global _session

    video_path = body.get("video_path", "")
    if not video_path or not Path(video_path).exists():
        return {"error": f"视频文件不存在：{video_path}"}

    if _session:
        await _session.stop()

    _session = GameSession(video_path)
    await _session.start()
    return {"status": "ok", "duration": _session.frame_pipe.duration_sec}


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
    前后端通信 WebSocket。

    前端 → 后端：
      - JSON: {"type": "seek",     "time": 12.5}
      - JSON: {"type": "playback", "action": "pause"|"resume"}
      - binary frame: PCM 16bit 音频数据（麦克风）

    后端 → 前端：
      - JSON: {"type": "tts",       "channel": "fast"|"slow"|"user_answer", "text": ..., "video_time": ...}
      - JSON: {"type": "perception", "intent": ..., "confidence": ..., ...}
      - JSON: {"type": "status",    "state": ..., ...}
      - JSON: {"type": "video_ended"}
    """
    await ws.accept()
    _ws_clients.append(ws)
    logger.info("WebSocket connected (total: %d)", len(_ws_clients))

    try:
        while True:
            msg = await ws.receive()

            if "bytes" in msg and msg["bytes"]:
                # 音频数据
                if _session:
                    _session.on_audio_chunk(msg["bytes"])

            elif "text" in msg and msg["text"]:
                import json
                try:
                    data = json.loads(msg["text"])
                    msg_type = data.get("type")

                    if msg_type == "seek" and _session:
                        await _session.on_seek(float(data["time"]))

                    elif msg_type == "playback" and _session:
                        if data.get("action") == "pause":
                            await _session.on_pause()
                        elif data.get("action") == "resume":
                            await _session.on_resume()

                except Exception as e:
                    logger.error("WS message error: %s", e)

    except WebSocketDisconnect:
        _ws_clients.remove(ws)
        logger.info("WebSocket disconnected (total: %d)", len(_ws_clients))


# ═══════════════════════════════════════════════════════════════════════
# 启动
# ═══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    uvicorn.run("backend.main:app", host="0.0.0.0", port=8000, reload=False)
