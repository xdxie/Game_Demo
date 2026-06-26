# NitroGen + VLM 游戏语音教练 Demo 设计文档

> 版本 0.2 | 2026-06-25（架构修订：Fix 11/13/14）

---

## 1. 项目概述

### 1.1 目标

构建一个游戏语音教练 Demo：用户导入一段游戏视频，系统自动播放并实时分析，通过语音向用户提供操作提示和策略建议。用户也可以随时开口提问，系统实时回答。

### 1.2 系统定位

```
快系统（NitroGen）  ────感知────→  当前帧该做什么（帧级直觉）
                                         │
                                    关键动作过滤
                                         │
                         ┌───────────────┴────────────────┐
                         │ 触发快通道                       │ 触发慢通道
                         ▼                                 ▼
                    模板→TTS播报                    VLM语义理解→TTS播报
                   （关键提示词）                  （策略建议/用户问答）

慢系统（VLM）      ────理解────→  为什么这么做（语义解释）
```

### 1.3 Demo 形态

- **输入**：本地游戏视频文件（.mp4/.avi 等），在浏览器内播放
- **帧采集**：前端 canvas 以 10fps 截取视频帧 → WebSocket 推送给后端 → NitroGen 推理
- **界面**：Web 页面，左侧播放视频，右侧显示语音文字记录
- **交互**：持续收音，用户开口即可提问，无需按任何按钮
- **输出**：TTS 语音通过浏览器扬声器播放 + 界面字幕同步显示

---

## 2. 整体架构

```
┌─────────────────────────────────────────────────────────────────────┐
│                          Frontend (Browser)                          │
│                                                                      │
│  ┌──────────────────┐    ┌──────────────────────────────────────┐   │
│  │   Video Player   │    │         Conversation Panel           │   │
│  │  (HTML5 video)   │    │  [字幕流 + 角色标签 + 时间戳]         │   │
│  └────────┬─────────┘    └──────────────────────────────────────┘   │
│           │ canvas 10fps截帧                                          │
│  ┌────────▼─────────────────────────────────────────────────────┐    │
│  │  Status Bar: [NitroGen ●] [VLM ●] [TTS ▶] [麦克风 🎤]        │    │
│  └──────────────────────────────────────────────────────────────┘    │
└────────┬────────────────────────────────┬────────────────────────────┘
         │ WS binary 0x02 (JPEG帧+时间)   │ WS binary 0x01 (PCM音频)
         │ WS JSON (seek/pause/resume)    │ WS binary (MP3 TTS音频 ←)
┌────────▼────────────────────────────────▼────────────────────────────┐
│                        Backend (Python / FastAPI)                     │
│                                                                       │
│  ┌────────────────┐    ┌──────────────────┐    ┌─────────────────┐   │
│  │  FrameBuffer   │    │  ActionFilter &  │    │  TTSQueue       │   │
│  │  接收前端推帧   │    │  EventDetector   │    │  单队列优先级    │   │
│  └───────┬────────┘    └────────┬─────────┘    └────────┬────────┘   │
│          │ PIL Image            │ GameEvent              │ on_audio   │
│          ▼                      ▼                        ▼  _data()  │
│  ┌───────────────┐    ┌──────────────────┐    ┌─────────────────┐    │
│  │ NitroGen      │    │  FastPath        │    │  TTSEngine      │    │
│  │ Client (ZMQ)  │    │  模板引擎         │    │  edge-tts合成   │    │
│  │               │    │  → 关键提示词     │    │  → MP3 bytes    │    │
│  └───────────────┘    └──────────────────┘    └─────────────────┘    │
│                                                                       │
│  ┌─────────────────────────────────────────────────────────────────┐ │
│  │  ASR Handler (Whisper)  ←──── 用户麦克风输入                     │ │
│  │  [VAD线程]  [转写线程 queue]  → on_utterance()                   │ │
│  └─────────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────┬────────────────────────────────────┘
                                   │ ZMQ REQ/REP
┌──────────────────────────────────▼────────────────────────────────────┐
│                      NitroGen Server (serve.py)                        │
│                      Linux / GPU 机器                                  │
└────────────────────────────────────────────────────────────────────────┘
```

### 2.1 进程划分

| 进程 | 运行环境 | 职责 |
|------|----------|------|
| `serve.py` | Linux + GPU | NitroGen 模型推理，ZMQ REP |
| `backend/main.py` | Windows / Python | 主控进程，FastAPI + WebSocket |
| Browser | 任意 | 视频播放 + canvas 帧捕获 + 麦克风采集 + TTS 音频播放 |

---

## 3. 目录结构

```
demo/
├── backend/
│   ├── main.py                  # FastAPI 入口，WebSocket 服务
│   ├── config.py                # 全局配置
│   │
│   ├── video/
│   │   ├── frame_buffer.py      # ★ 接收前端推帧（Fix 11），供 NitroGen 读取
│   │   └── frame_pipe.py        # 备用：cv2 本地读帧（当前未被主流程使用）
│   │
│   ├── nitrogen/
│   │   ├── client.py            # ZMQ 客户端，封装 predict()
│   │   └── parser.py            # action vector → PerceptionSignal
│   │
│   ├── fast/
│   │   ├── action_filter.py     # 动作突变检测，关键动作识别，冷却管理
│   │   ├── templates.py         # PerceptionSignal → 提示文本
│   │   └── event.py             # GameEvent 数据结构定义
│   │
│   ├── slow/
│   │   ├── vlm_client.py        # Claude API 调用，prompt 管理
│   │   ├── context_buffer.py    # 近期动作序列 + 事件历史
│   │   └── trigger.py           # 慢系统触发判断逻辑
│   │
│   ├── tts/
│   │   ├── queue.py             # 优先级 TTS 队列，打断机制
│   │   └── engine.py            # TTS 合成，MP3 bytes 发往前端（Fix 14）
│   │
│   └── asr/
│       └── handler.py           # Whisper 语音识别，VAD，独立转写线程（Fix 13）
│
├── frontend/
│   ├── index.html
│   ├── style.css
│   └── app.js                   # WebSocket 客户端，视频控制，麦克风
│
├── scripts/
│   └── serve.py                 # NitroGen 原始 serve（直接用上游代码）
│
├── requirements.txt
└── .env                         # API keys, 配置
```

---

## 4. 核心模块详细设计

### 4.1 FrameBuffer（前端推帧缓冲，Fix 11）

**职责**：接收前端通过 WebSocket 推送的视频帧，缓存最新一帧供 NitroGen 读取。
接口与原 `VideoFramePipe` 完全兼容，`NitroGenClient` 无需改动。

**为什么改为前端推帧？**

原方案后端用 `cv2.VideoCapture` 独立重读视频文件，与前端 HTML5 播放器时间轴不同步，
正常播放会产生累积误差，seek 后也需要额外同步机制。
改为前端推帧后，NitroGen 接收的永远是用户当前看到的那一帧，时间戳精确。

**前端帧捕获流程（`frontend/app.js`）**：

```
ws.onopen 后
  → setInterval(100ms)
      → captureCtx.drawImage(videoPlayer, 0, 0, 256, 256)
      → canvas.toBlob(JPEG, quality=0.85)
      → 构造 WebSocket 二进制消息：
          [byte 0x02][float64 LE 视频时间][JPEG bytes]
      → ws.send(msg)
```

**后端接收（`backend/main.py` WebSocket 处理）**：

```python
msg_type  = data[0]           # 0x02
video_time = struct.unpack_from("<d", data, 1)[0]  # 8字节 float64
jpeg_bytes = data[9:]
session.on_video_frame(jpeg_bytes, video_time)
# → frame_buffer.push(jpeg_bytes, video_time)
```

**FrameBuffer 接口**（`backend/video/frame_buffer.py`）：

```python
class FrameBuffer:
    latest_frame:   Optional[PIL.Image]  # NitroGenClient 读此属性
    video_position: float                # 当前视频时间（秒）
    duration_sec:   float                # 由前端 video_ready 消息设置

    def push(self, jpeg_bytes: bytes, video_time: float): ...
    def pause(self) / resume(self): ...
    def seek(self, _time): ...  # 清空 latest_frame，防止旧帧重复推理
```

> **注**：`backend/video/frame_pipe.py`（cv2 本地读帧）保留为备用，当前主流程不使用。

---

### 4.2 NitroGenClient（推理客户端）

**职责**：封装与 serve.py 的 ZMQ 通信，异步推理，对外暴露最新 chunk。

```python
# backend/nitrogen/client.py

class NitroGenClient:
    def __init__(self, server_addr: str = "tcp://localhost:5555"):
        self.socket = zmq.Context().socket(zmq.REQ)
        self.socket.connect(server_addr)

        # 最新推理结果（线程安全）
        self._latest_chunk: Optional[dict] = None
        self._chunk_lock = threading.Lock()
        self._running = False

    def start(self, frame_pipe: VideoFramePipe):
        """启动推理循环"""
        self._running = True
        threading.Thread(target=self._inference_loop,
                         args=(frame_pipe,), daemon=True).start()

    def _inference_loop(self, frame_pipe: VideoFramePipe):
        while self._running:
            frame = frame_pipe.latest_frame
            if frame is None:
                time.sleep(0.01)
                continue

            # ZMQ 请求（阻塞，约 200ms）
            self.socket.send(pickle.dumps({
                "type": "predict",
                "image": frame
            }))
            response = pickle.loads(self.socket.recv())

            with self._chunk_lock:
                self._latest_chunk = response["pred"]

    def get_latest_chunk(self) -> Optional[dict]:
        with self._chunk_lock:
            return self._latest_chunk
```

---

### 4.3 ActionFilter & EventDetector（快系统核心）

**职责**：消费 NitroGen chunk，检测"关键动作"和"动作突变"，输出 GameEvent。

#### 关键动作定义

```python
# backend/fast/event.py

from enum import Enum
from dataclasses import dataclass

class EventType(Enum):
    # 快系统事件（来自 NitroGen）
    SUDDEN_DODGE        = "sudden_dodge"        # 突发闪避（高置信 + 突变）
    ATTACK_WINDOW       = "attack_window"       # 攻击窗口开启（从防御切攻击）
    SUSTAINED_DANGER    = "sustained_danger"    # 持续危险（长时间高置信 DODGE）
    MOVEMENT_SHIFT      = "movement_shift"      # 移动方向突变

    # 慢系统触发事件
    PATTERN_COMPLETED   = "pattern_completed"   # 一段连续操作结束（NitroGen WAIT）
    SUSTAINED_DIVERGENCE= "sustained_divergence"# 用户与 AI 长时间背离（未来扩展）
    USER_QUESTION       = "user_question"       # 用户主动提问

@dataclass
class GameEvent:
    type: EventType
    timestamp: float          # 视频时间轴时间（秒）
    perception: "PerceptionSignal"
    trigger_fast: bool        # 是否触发快通道
    trigger_slow: bool        # 是否触发慢通道
    user_text: str = ""       # 用户提问内容（USER_QUESTION 时使用）
```

#### 过滤与触发逻辑

```python
# backend/fast/action_filter.py

class ActionFilter:
    def __init__(self):
        # 每类事件的冷却时间（秒）
        self.COOLDOWNS = {
            EventType.SUDDEN_DODGE:     3.0,
            EventType.ATTACK_WINDOW:    4.0,
            EventType.SUSTAINED_DANGER: 8.0,
            EventType.MOVEMENT_SHIFT:  10.0,
            EventType.PATTERN_COMPLETED: 5.0,
        }
        self._last_trigger: dict = {}   # EventType → 上次触发时间
        self._prev_signal: Optional[PerceptionSignal] = None

        # 用于 PATTERN_COMPLETED 检测
        self._active_pattern_start: float = 0.0
        self._active_pattern_type: str = "WAIT"

    def process(self, signal: PerceptionSignal, video_time: float) -> Optional[GameEvent]:
        """
        每次收到新 chunk 时调用（约 10fps）
        返回 None 表示无需触发
        """
        event = self._detect(signal, video_time)
        if event is None:
            return None

        # 冷却检查
        last = self._last_trigger.get(event.type, 0.0)
        if video_time - last < self.COOLDOWNS.get(event.type, 3.0):
            return None

        self._last_trigger[event.type] = video_time
        self._prev_signal = signal
        return event

    def _detect(self, signal: PerceptionSignal, t: float) -> Optional[GameEvent]:
        prev = self._prev_signal

        # ── 检测1：突发闪避 ──────────────────────────────────────
        # 条件：本 chunk 主导意图是 DODGE，置信度 > 0.75
        #       且上一个 chunk 的主导意图不是 DODGE（突变）
        if (signal.primary_intent == "DODGE"
                and signal.confidence > 0.75
                and (prev is None or prev.primary_intent != "DODGE")):
            return GameEvent(
                type=EventType.SUDDEN_DODGE,
                timestamp=t,
                perception=signal,
                trigger_fast=True,
                trigger_slow=False,  # 快通道独立处理
            )

        # ── 检测2：攻击窗口 ──────────────────────────────────────
        # 条件：从 DODGE/GUARD 切换到 ATTACK，且 ATTACK 置信度 > 0.7
        if (signal.primary_intent == "ATTACK"
                and signal.confidence > 0.7
                and prev is not None
                and prev.primary_intent in ("DODGE", "GUARD")):
            return GameEvent(
                type=EventType.ATTACK_WINDOW,
                timestamp=t,
                perception=signal,
                trigger_fast=True,
                trigger_slow=True,   # 同时触发慢系统（并行）
            )

        # ── 检测3：持续危险 ──────────────────────────────────────
        # 条件：DODGE 主导意图持续超过 3 秒
        if (signal.primary_intent == "DODGE"
                and signal.confidence > 0.6):
            if self._active_pattern_type == "DODGE":
                duration = t - self._active_pattern_start
                if duration > 3.0:
                    return GameEvent(
                        type=EventType.SUSTAINED_DANGER,
                        timestamp=t,
                        perception=signal,
                        trigger_fast=True,
                        trigger_slow=True,
                    )
            else:
                self._active_pattern_start = t
                self._active_pattern_type = "DODGE"

        # ── 检测4：操作段结束（NitroGen 进入 WAIT）────────────────
        # 条件：上一 chunk 是战斗意图，本 chunk 切换到 WAIT/NAVIGATE
        non_combat = {"WAIT", "NAVIGATE"}
        was_combat = prev and prev.primary_intent not in non_combat
        now_idle   = signal.primary_intent in non_combat

        if was_combat and now_idle:
            self._active_pattern_start = t
            self._active_pattern_type = "WAIT"
            return GameEvent(
                type=EventType.PATTERN_COMPLETED,
                timestamp=t,
                perception=signal,
                trigger_fast=False,
                trigger_slow=True,   # 只触发慢系统总结
            )

        return None
```

---

### 4.4 FastPath 模板引擎

**职责**：将 GameEvent 转换为简短提示文本。纯模板，不调用 LLM，延迟 <1ms。

```python
# backend/fast/templates.py

DIRECTION_ZH = {
    "LEFT": "向左", "RIGHT": "向右",
    "FORWARD": "向前", "BACK": "向后", None: ""
}

FAST_TEMPLATES = {
    EventType.SUDDEN_DODGE: [
        lambda s: f"{DIRECTION_ZH[s.move_direction]}闪！",          # 有方向
        lambda s: "注意，快闪！",                                      # 无方向
    ],
    EventType.ATTACK_WINDOW: [
        lambda s: "有机会，打！",
        lambda s: "进攻！",
    ],
    EventType.SUSTAINED_DANGER: [
        lambda s: "持续危险，保持闪避",
        lambda s: "这段很危险，别停",
    ],
    EventType.MOVEMENT_SHIFT: [
        lambda s: f"AI 建议{DIRECTION_ZH[s.move_direction]}移动",
    ],
}

def render_fast(event: GameEvent) -> str:
    templates = FAST_TEMPLATES.get(event.type, [lambda s: "注意！"])
    # 有方向信息用第一个模板，没有用第二个
    idx = 0 if event.perception.move_direction else min(1, len(templates)-1)
    return templates[idx](event.perception)
```

**设计原则**：
- 快通道文本 ≤ 8 字
- 包含具体方向（来自 NitroGen j_left 输出）
- 不包含任何需要理解的解释

---

### 4.5 SlowPath VLM 客户端

**职责**：接收触发事件 + 游戏画面 + 动作上下文，调用 Claude API，生成语义建议。

#### 触发条件

```
触发来源1：ActionFilter 输出的 trigger_slow=True 事件
           → 并行处理（不等快通道播完）
           → 如果快通道正在播，VLM 结果放入队列等待

触发来源2：用户主动提问（USER_QUESTION 事件）
           → 最高优先级，打断当前 TTS
           → 清空队列中所有 trigger_slow 结果（过期了）

触发来源3：PATTERN_COMPLETED（一段操作结束后总结）
           → 中等优先级
```

#### ContextBuffer（动作上下文）

```python
# backend/slow/context_buffer.py

class ContextBuffer:
    """
    维护近期的感知信号序列，供 VLM 理解"刚才发生了什么"
    不存原始 chunk，存压缩后的意图序列
    """

    def __init__(self, window_sec: float = 15.0):
        self.window_sec = window_sec
        self._entries: deque = deque()  # (timestamp, PerceptionSignal)
        self._events:  deque = deque()  # (timestamp, GameEvent)

    def push_signal(self, t: float, signal: PerceptionSignal):
        self._entries.append((t, signal))
        self._evict(t)

    def push_event(self, t: float, event: GameEvent):
        self._events.append((t, event))

    def _evict(self, now: float):
        while self._entries and now - self._entries[0][0] > self.window_sec:
            self._entries.popleft()

    def summarize(self) -> str:
        """
        输出供 VLM 使用的上下文描述
        格式：压缩的意图序列 + 关键事件列表
        """
        if not self._entries:
            return "无近期动作记录"

        # 压缩意图序列（run-length encoding）
        intents = [s.primary_intent for _, s in self._entries]
        compressed = _run_length(intents)
        # e.g. "NAVIGATE×8 → DODGE×5 → ATTACK×6 → DODGE×3"

        # 近期关键事件
        recent_events = [
            f"[{t:.1f}s] {e.type.value}"
            for t, e in self._events
            if self._entries and t >= self._entries[0][0]
        ]

        return (
            f"近{self.window_sec:.0f}秒动作序列：{compressed}\n"
            f"关键事件：{', '.join(recent_events) or '无'}"
        )
```

#### ConversationHistory（多轮对话历史）

```python
# backend/slow/context_buffer.py（续）

class ConversationHistory:
    """
    维护用户与 VLM 的多轮对话历史，专用于 USER_QUESTION 触发的问答。
    事件驱动的慢通道建议（ATTACK_WINDOW、PATTERN_COMPLETED 等）不计入此历史。
    目的：让用户能追问"那之前呢？"、"再说详细点"，VLM 有上下文可用。
    """
    MAX_TURNS = 5   # 最多保留 5 轮问答，防止 context 过长

    def __init__(self):
        self._turns: list[tuple[str, str]] = []   # [(user_text, ai_response), ...]

    def add_turn(self, user_text: str, ai_response: str):
        self._turns.append((user_text, ai_response))
        if len(self._turns) > self.MAX_TURNS:
            self._turns.pop(0)  # 滚动丢弃最旧的

    def to_messages(self) -> list[dict]:
        """转换为 Claude API messages 格式（历史轮，不含当前轮）"""
        messages = []
        for user_text, ai_text in self._turns:
            messages.append({"role": "user",      "content": user_text})
            messages.append({"role": "assistant", "content": ai_text})
        return messages

    def clear(self):
        """用户主动清空或收到 clear_conversation 时调用；seek 时保留（见 5.6）"""
        self._turns.clear()
```

**设计说明**：
- 事件驱动的慢通道建议（如 "Boss右拳，左侧跑开更安全"）**不**写入 ConversationHistory
  - 原因：这类建议是单向播报，没有对应用户提问，放入历史会干扰 VLM 理解用户意图
- 只有 `USER_QUESTION → VLM → answer` 这条链路产生的对话才写入
- `to_messages()` 在 VLM 调用时作为 `messages` 数组的历史前缀

#### FastHistory（快通道历史追踪）

```python
# backend/slow/context_buffer.py（续）

class FastHistory:
    """
    记录近期快通道已播报的内容，供慢系统 VLM 生成时避免内容重复。
    快通道说过"向左闪！"，慢通道就不用再重复方向提示了。
    """
    EXPIRE_SEC = 10.0   # 快通道提示 10 秒后认为用户已遗忘，慢通道不再刻意回避

    def __init__(self):
        self._records: deque[tuple[float, str]] = deque()  # (video_time, text)

    def record(self, video_time: float, text: str):
        self._records.append((video_time, text))

    def get_recent_summary(self, current_time: float, max_items: int = 3) -> str:
        """
        返回未过期的近期快通道提示，注入 VLM prompt 的 "刚才快通道已播报" 字段
        格式示例："向左闪！、有机会打！"
        """
        recent = [
            text for ts, text in self._records
            if current_time - ts < self.EXPIRE_SEC
        ]
        if not recent:
            return "无"
        return "、".join(recent[-max_items:])

    def clear(self):
        self._records.clear()
```

#### 三个 Context 对象的数据流

```
数据写入：
  VideoFramePipe ──帧──→ ContextBuffer.push_signal()      ← 每帧更新（10fps）
  ActionFilter   ──事件→ ContextBuffer.push_event()       ← 关键事件更新
  FastPath 播报  ──文本→ FastHistory.record(video_time)   ← 快通道文本入队时写入
  VLM 回答完成   ──对话→ ConversationHistory.add_turn()   ← USER_QUESTION 响应完成时写入

VLM 调用时读取：
  context_buffer.summarize()                    → 近期动作序列描述（喂给 VLM 的主要感知上下文）
  fast_history.get_recent_summary(video_time)   → 快通道已播内容（告知 VLM 不要重复）
  conversation_history.to_messages()            → 历史问答前缀（仅 USER_QUESTION 路径使用）
```

---

#### VLM Prompt 设计

```python
# backend/slow/vlm_client.py

SYSTEM_PROMPT = """你是一个游戏语音教练，正在实时分析玩家的游戏视频。
旁边有一个 AI 系统（NitroGen）在分析每一帧画面，给出它认为的最优动作。

你的职责：
- 基于画面和 NitroGen 的感知信号，给玩家提供有价值的建议
- 回答要口语化、简短（1-2句话，不超过 40 字）
- 不要重复刚才快通道已经说过的内容（会在上下文中告知）
- 语气像一个有经验的老玩家在旁边指导，有时鼓励，有时提醒

约束：
- 不用列表，不用 Markdown
- 如果信息不足，给出最合理的推断，不要说"我不确定"
- 不超过 40 字"""


async def call_vlm(
    event: GameEvent,
    frame: PIL.Image,
    context: ContextBuffer,
    last_fast_text: str,
    user_question: str = "",
    conversation_history: list = [],
) -> str:

    signal = event.perception
    ctx_summary = context.summarize()

    # 把 PIL Image 转 base64
    img_b64 = pil_to_base64(frame)

    if user_question:
        task_desc = f"用户提问：{user_question}"
        guidance  = "直接回答用户问题，结合当前画面和 NitroGen 感知信号。"
    else:
        task_desc = f"触发原因：{event.type.value}"
        guidance  = "给出这个局面下最有价值的一句建议，不要重复刚才已说的内容。"

    user_msg = f"""{ctx_summary}

NitroGen 当前感知：
- 主导意图：{signal.primary_intent}（置信度 {signal.confidence:.0%}）
- 方向：{signal.move_direction or '无'}
- 未来预测序列：{'→'.join(signal.horizon_sequence)}

刚才快通道已播报："{last_fast_text or '无'}"

{task_desc}
{guidance}"""

    messages = conversation_history + [{
        "role": "user",
        "content": [
            {"type": "image", "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": img_b64
            }},
            {"type": "text", "text": user_msg}
        ]
    }]

    response = anthropic.Anthropic().messages.create(
        model="claude-sonnet-4-6",
        max_tokens=120,
        system=SYSTEM_PROMPT,
        messages=messages,
    )
    return response.content[0].text.strip()
```

---

### 4.6 TTS 队列与调度（Fix 14）

**核心约束**：同一时间只能播报一条语音。

**Fix 14 变化**：TTS 不再在服务端 pygame 播放，改为将合成的 MP3 bytes 通过 WebSocket 发送给前端，由浏览器 Audio API 播放。`on_complete` 改为基于音频时长估算定时触发（不再等待本地播放结束）。

```
TTS 播报新流程：

TTSQueue._speak_next()
  → ASRHandler.mute()
  → TTSEngine.speak_async(text, on_complete)
      ├─ [合成线程] edge-tts 合成 → MP3 bytes
      ├─ on_audio_data(mp3_bytes) → WebSocket.send_bytes() → 前端播放
      └─ 估算时长（pydub）→ Timer(duration, on_complete)
                                     ↓
                              TTSQueue._on_complete()
                                → ASRHandler.unmute()
                                → Timer(inter_gap, _speak_next)
```

优先级与过期规则不变：

```python
class Priority(IntEnum):
    USER_ANSWER  = 0   # 最高，打断当前播报
    FAST_HINT    = 1   # 2 秒内未播则丢弃
    SLOW_ADVICE  = 2   # 8 秒内未播则丢弃
    SLOW_SUMMARY = 3   # 15 秒内未播则丢弃
```

**TTSEngine 关键变化**（`backend/tts/engine.py`）：

```python
class TTSEngine:
    # Fix 14：不再有 pygame 播放，改为回调
    on_audio_data: Optional[Callable[[bytes], None]] = None

    def speak_async(self, text, on_complete=None):
        # 1. 合成 → MP3 bytes
        # 2. on_audio_data(bytes)  ← 触发 WebSocket 广播
        # 3. 估算时长 → Timer(duration, on_complete)
        ...

    @staticmethod
    def _estimate_duration(audio_data: bytes) -> float:
        # 优先用 pydub 精确解析，+0.3s 缓冲
        seg = AudioSegment.from_mp3(io.BytesIO(audio_data))
        return len(seg) / 1000.0 + 0.3
```

**前端 TTS 播放**（`frontend/app.js`）：

```javascript
// 解析 0x03 帧：byte[1:5]=utterance_id，byte[5:]=MP3
const parsed = parseTTSBinaryFrame(e.data);
const audio = new Audio(URL.createObjectURL(new Blob([parsed.mp3], {type:'audio/mpeg'})));
audio.onended = () => ws.send(JSON.stringify({
    type: 'tts_done',
    utterance_id: parsed.utteranceId,
}));
audio.play();
```

仅主连接（`session_role: primary`）接收 MP3 并回传 `tts_done`。

---

### 4.7 ASR 用户语音输入（Fix 13）

系统持续收音，VAD 检测到用户说话结束后触发 Whisper 识别，识别结果直接触发慢系统回复。无需任何按钮。

**Fix 13 变化**：`_flush()` 改为非阻塞，将音频放入 `Queue` 后立即返回。独立的转写线程持续从队列消费，运行 `whisper.transcribe()`。VAD 与 Whisper 并发运行，不会漏捕连续语音。

```
WebSocket 协程（VAD）              转写线程
       │                          │
       │── process_audio_chunk() →│
       │                          │── amplitude > threshold  │
       │                          │── 静音超时               │
       │                          │── _flush()               │
       │                          │    └─ queue.put(arr) →→→→│── transcribe()
       │                          │── _reset_vad()           │── on_utterance(text)
       │                          │   （立即可捕新语音）       │
```

```python
class ASRHandler:
    # VAD 参数（5号调优）
    SILENCE_THRESHOLD = 300
    SPEECH_MIN_SEC    = 0.5
    SILENCE_END_SEC   = 1.2
    TTS_MUTE_TAIL_SEC = 0.2

    def __init__(self, ...):
        # Fix 13：独立转写线程
        self._transcription_queue = queue.Queue(maxsize=4)
        self._transcription_thread = threading.Thread(
            target=self._transcription_loop, daemon=True
        )
        self._transcription_thread.start()

    def _flush(self):
        """非阻塞：仅入队，立即重置 VAD"""
        arr = np.frombuffer(b"".join(self._audio_buffer), dtype=np.int16)\
                .astype(np.float32) / 32768.0
        self._transcription_queue.put_nowait(arr)   # 不阻塞

    def _transcription_loop(self):
        """独立线程：阻塞在队列，运行 Whisper"""
        while True:
            arr = self._transcription_queue.get()
            if arr is None: break
            result = self.model.transcribe(arr, language=self.language, fp16=False)
            text = result["text"].strip()
            if text and self.on_utterance:
                self.on_utterance(text)
```

**TTS 播报期间的 mute/unmute 机制不变**（详见 Fix 14 流程图）。
        self._is_speaking = False
        threading.Timer(0.8, self._speak_next).start()

    def _interrupt(self):
        self._tts.stop()
        self._asr.unmute()        # 打断时也恢复 ASR
        self._is_speaking = False
```

---

## 5. 事件流与时序

### 5.1 正常触发流（无用户提问）

```
Video T=5.3s
  → Frame 提取 → NitroGen 推理（200ms）
  → T=5.5s: chunk 返回，ActionFilter.process()
  → 检测到 SUDDEN_DODGE（突变 + 高置信）
  → 冷却通过
  
  快通道（同步）:
    render_fast() → "向左闪！"
    TTSQueue.push("向左闪！", FAST_HINT)
    → TTS 立即播放（200ms）
  
  慢通道（并行异步）:
    call_vlm(event, frame, context) → 约 1-2s
    → "Boss右拳，左侧跑开更安全"
    TTSQueue.push(..., SLOW_ADVICE)
    → 快通道播完后播放（如未过期）
```

### 5.2 用户提问流

```
系统持续收音（VAD 运行中）

用户说："这段该怎么打？"
  → VAD 检测到语音开始
  → TTS 正在播报？→ 已被 mute，本次不处理（用户说话时 TTS 继续）
  → TTS 未播报？→ 正常录音
  → VAD 检测到静音 1.2 秒 → 说话结束
  → Whisper 识别 → "这段该怎么打？"
  → 触发 USER_QUESTION 事件
  → TTSQueue 打断当前播报（如有）
  → 清空队列中所有 SLOW_ADVICE（已过期）
  → call_vlm(user_question="这段该怎么打？", ...) → 约 1s
  → TTSQueue.push(answer, USER_ANSWER)  ← 最高优先级
  → 立即播放
```

**设计说明**：用户说话时 TTS 恰好在播，有两种处理选择：
- **方案 A（文档采用）**：TTS 期间 ASR mute，用户这句话丢弃。用户说完后 TTS 结束，下次再说即可响应。适合 demo 展示，逻辑简单。
- **方案 B**：TTS 期间也录音，说话结束后先停 TTS 再回答。体验更自然但实现复杂。如需升级可替换 `mute()` 逻辑。

### 5.3 操作段结束流（PATTERN_COMPLETED）

```
NitroGen 连续输出 WAIT/NAVIGATE（战斗结束）
  → ActionFilter 检测到 PATTERN_COMPLETED
  → 只触发慢通道
  → call_vlm(event=PATTERN_COMPLETED, context=最近15秒)
  → "刚才那段 AI 一直在用左侧绕圈，这样可以规避 Boss 的范围攻击"
  → TTSQueue.push(..., SLOW_SUMMARY)  ← 最低优先级，有空才播
```

---

### 5.4 系统状态机

系统存在四个相互影响的状态轴，各自独立管理，但有明确的交互约束：

```
TTS 状态：   IDLE
             → PLAYING_FAST（快通道播报）
             → PLAYING_SLOW（慢通道建议）
             → PLAYING_ANSWER（用户问答回复）
             USER_ANSWER 可打断其他任何状态

VLM 状态：   IDLE
             → IN_FLIGHT（请求发出，等待响应）
             → CANCELLED（被 USER_QUESTION 取消）
             同时最多 1 个 in-flight + 1 个 pending

ASR 状态：   LISTENING（VAD 运行，等待语音）
             → RECORDING（VAD 触发，正在录音缓冲）
             → PROCESSING（Whisper 识别中）
             → MUTED（TTS 播报期间，VAD 暂停）

视频状态：   PLAYING → PAUSED → PLAYING
                     → SEEKING → PLAYING（含全系统重置）
```

**快慢通道交互优先级规则：**

| 新事件 | TTS 当前状态 | 处理逻辑 |
|--------|------------|---------|
| FAST_HINT 到达 | IDLE | 立即入队播报 |
| FAST_HINT 到达 | PLAYING_SLOW / PLAYING_SUMMARY | 进入队列（比慢通道优先）；2 秒内未播则丢弃 |
| FAST_HINT 到达 | PLAYING_ANSWER | 进入队列；2 秒内未播则丢弃（不打断用户回答） |
| SLOW_ADVICE 到达 | IDLE | 立即入队播报 |
| SLOW_ADVICE 到达 | PLAYING_* | 进入队列，按优先级排序 |
| USER_ANSWER 到达 | PLAYING_* | **立即打断**，插队最前播报 |
| VIDEO PAUSE | PLAYING_* | 暂停 TTS；保留队列（恢复后继续） |
| VIDEO SEEK | 任意 | 全系统重置（详见 5.6） |

**快通道不打断规则说明**：

FAST_HINT 有 2 秒有效期，过期自动丢弃。慢通道正在播报时，快提示进队等待；若慢通道 2 秒内播完则还能播出，超过 2 秒则静默丢弃。这是故意为之：超过 2 秒的"向左闪！"已无操作价值，强行打断慢通道反而体验更差。

---

### 5.5 VLM 请求生命周期管理

```python
# backend/slow/trigger.py

class VLMRequestManager:
    """
    管理慢系统 VLM 请求的完整生命周期。

    核心约束：
    - 同一时刻最多 1 个 VLM 请求 in-flight
    - 最多缓冲 1 个 pending 请求（新的高优先级请求替换旧的）
    - USER_QUESTION：取消当前 in-flight，清空慢通道 TTS 队列，立即提交
    - 同类事件在 VLM_DEDUP_SEC 内不重复提交
    """

    VLM_DEDUP_SEC = 5.0   # 同类事件去重窗口（秒）

    def __init__(self, tts_queue, context_buffer, fast_history, conversation_history):
        self._tts       = tts_queue
        self._ctx       = context_buffer
        self._fast_hist = fast_history
        self._conv_hist = conversation_history

        self._current_task: Optional[asyncio.Task] = None
        self._pending: Optional[dict] = None

        self._last_event_type:  Optional[EventType] = None
        self._last_submit_time: float = 0.0

    async def submit(self, event: GameEvent, frame: PIL.Image):
        """提交 VLM 请求（非阻塞，立即返回）"""
        priority = self._event_to_priority(event)
        is_user_q = (event.type == EventType.USER_QUESTION)
        now = time.time()

        # ── 去重（用户提问不去重）────────────────────────────────
        if (not is_user_q
                and event.type == self._last_event_type
                and now - self._last_submit_time < self.VLM_DEDUP_SEC):
            return

        # ── USER_QUESTION：取消当前，清空慢通道 TTS 队列 ─────────
        if is_user_q:
            if self._current_task and not self._current_task.done():
                self._current_task.cancel()
            self._pending = None
            # 清空 TTS 队列里已过期的慢通道内容（用户当前最关心的是自己的问题）
            self._tts.clear_by_priority([Priority.SLOW_ADVICE, Priority.SLOW_SUMMARY])

        # ── 构造任务参数（快照当前上下文，防止异步读到更新后的状态）──
        task_args = {
            "event":        event,
            "frame":        frame,
            "priority":     priority,
            "ctx_snapshot": self._ctx.summarize(),
            "fast_recent":  self._fast_hist.get_recent_summary(event.timestamp),
            "conv_messages": self._conv_hist.to_messages() if is_user_q else [],
        }

        # ── 提交 ──────────────────────────────────────────────────
        if self._current_task is None or self._current_task.done():
            self._current_task = asyncio.create_task(self._run(task_args))
        else:
            # 已有请求 in-flight：只保留最新/最高优先级的 pending
            if (self._pending is None
                    or priority <= self._get_pending_priority()):
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
            )

            self._last_event_type  = event.type
            self._last_submit_time = time.time()

            self._tts.push(text, args["priority"])

            # 用户问答写入对话历史（事件驱动建议不写入）
            if is_user_q:
                self._conv_hist.add_turn(event.user_text, text)

        except asyncio.CancelledError:
            pass  # 被 USER_QUESTION 取消，静默丢弃，不播报

        finally:
            # 无论成功/取消，检查是否有 pending 需要继续
            if self._pending:
                pending = self._pending
                self._pending = None
                self._current_task = asyncio.create_task(self._run(pending))

    async def cancel_all(self):
        """视频 seek 时调用：取消所有请求"""
        if self._current_task and not self._current_task.done():
            self._current_task.cancel()
            try:
                await self._current_task
            except asyncio.CancelledError:
                pass
        self._pending = None

    def _event_to_priority(self, event: GameEvent) -> Priority:
        if event.type == EventType.USER_QUESTION:
            return Priority.USER_ANSWER
        if event.type == EventType.PATTERN_COMPLETED:
            return Priority.SLOW_SUMMARY
        return Priority.SLOW_ADVICE

    def _get_pending_priority(self) -> int:
        if self._pending is None:
            return 999
        return int(self._pending["priority"])
```

**VLM 请求状态转移图：**

```
IDLE
 │  submit(任意事件)
 ▼
IN_FLIGHT ─────────────→ 完成 → tts.push() → IDLE
 │  \                                            ↑
 │   submit(普通事件)                            │ pending 自动触发
 │     ↓                                        │
 │   PENDING（最多1个，高优先级可替换）──────────→┘
 │
 │  submit(USER_QUESTION)
 ▼
CANCELLING → cancel() 完成 → 立即启动 USER_QUESTION 请求
```

---

### 5.6 视频 Seek 时的全系统状态重置

拖动进度条是状态一致性最复杂的场景。所有基于视频时间轴构建的上下文（动作序列、触发事件历史、快通道记录）都必须失效，防止旧时间点的感知信号污染新位置的判断。

```python
# backend/main.py（节选）

async def on_video_seek(self, new_time: float):
    """
    前端发送 {"type": "seek", "time": 12.5} 时触发
    执行完整的系统状态重置，然后从新位置继续
    """

    # Step 1：暂停推理，防止 seek 期间有新 chunk 进来污染状态
    self.nitrogen_client.pause()

    # Step 2：停止 TTS，清空队列（旧位置的播报内容已无意义）
    self.tts_queue.clear_and_stop()

    # Step 3：ASR 恢复收音（TTS 已停，跳过 tail delay 直接 unmute）
    self.asr_handler.force_unmute()

    # Step 4：取消所有 VLM 请求（旧位置的 VLM 结果已过时）
    await self.vlm_manager.cancel_all()

    # Step 5：清空时间相关上下文
    self.context_buffer.clear()    # 动作序列：旧位置序列对新位置无意义
    self.fast_history.clear()      # 快通道记录：旧位置提示对新位置无参考价值
    self.action_filter.reset()     # 过滤器状态：_prev_signal = None，防止新位置
                                   # 第一帧被误判为"突变"

    # Step 6：ConversationHistory 保留（不清空）
    # 理由：用户可能 seek 回去说"刚才那段我没听清，再说一下"
    # 风险：旧位置问答对新位置语义可能不连贯
    # 折中：保留历史，VLM 本身能从画面感知当前位置，不会被旧对话误导太多

    # Step 7：帧缓冲重置（清空旧帧，Fix 11）
    self.frame_buffer.seek(new_time)

    # Step 8：恢复推理
    self.nitrogen_client.resume()
```

**Seek 前后各对象状态对比：**

| 状态对象 | Seek 后处理 | 理由 |
|---------|-----------|------|
| `ContextBuffer`（动作序列） | **清空** | 基于视频时间轴，旧位置序列无意义 |
| `FastHistory`（快通道记录） | **清空** | 旧位置的提示文本对新位置无参考价值 |
| `ActionFilter._prev_signal` | **重置为 None** | 防止新位置首帧被误判为突变 |
| `ActionFilter._last_trigger` | **保留**（冷却时间跨 seek 保留） | 防止进度条反复拖动触发密集播报 |
| `ConversationHistory` | **保留** | 用户可能 seek 后追问旧话题 |
| `TTS 队列` | **清空并停止** | 旧位置的播报内容已过时 |
| `VLM in-flight/pending` | **取消** | 旧位置的 VLM 请求结果已过时 |
| `FrameBuffer.latest_frame` | **清空** | 防止旧帧被 NitroGen 重复推理 |

---

## 6. 前后端接口设计

### 6.1 HTTP API

| 端点 | 方法 | 说明 |
|------|------|------|
| `/start` | POST | 启动分析会话（无需传视频路径，Fix 11） |
| `/stop`  | POST | 停止分析 |

### 6.2 WebSocket 消息协议（Fix 11、14 后更新）

#### 二进制消息（客户端 → 服务端）

```
byte[0] = 0x01  PCM 音频（麦克风，用于 ASR）
          byte[1:] = PCM int16 little-endian，16kHz，约 100ms/块

byte[0] = 0x02  视频帧（canvas 截图，用于 NitroGen，Fix 11）
          byte[1:9] = float64 little-endian（视频当前时间，秒）
          byte[9:]  = JPEG bytes，256×256
```

#### 二进制消息（服务端 → 客户端）

```
byte[0]    = 0x03  TTS 音频帧（utterance 握手）
byte[1:5]  = uint32 little-endian（utterance_id，与 JSON tts 消息对应）
byte[5:]   = MP3 bytes

前端解析 0x03 帧后播放 MP3，Audio.onended 时发送 tts_done。
即使 JSON 字幕晚于 MP3 到达，也可从帧头取得 utterance_id。
```

#### JSON 消息（客户端 → 服务端）

```json
// 连接后首条消息：注册角色（首个 player 成为主连接）
{ "type": "register", "role": "player" | "observer" }

// 视频元数据（主连接在 video_ready 前需已完成 register）
{ "type": "video_ready", "duration": 120.5 }

// 视频进度同步（仅主连接）
{ "type": "seek",     "time": 12.5 }

// 暂停/继续（仅主连接）
{ "type": "playback", "action": "pause" | "resume" }

// 视频自然结束（仅主连接）
{ "type": "video_ended" }

// 清空多轮对话历史（仅主连接）
{ "type": "clear_conversation" }

// TTS 播放完毕（仅主连接）
{ "type": "tts_done", "utterance_id": 42 }
```

**主连接模型**：首个注册为 `player` 的客户端成为主连接，负责推帧、麦克风、seek 与 `tts_done`。`observer` 仅接收 JSON 字幕/状态。主连接断开后，下一个 `player` 被提升为主连接（收到 `session_role: primary` 与全局 `primary_changed`）。

主连接接管或 WS 重连时，前端应发送 `video_ready` + `seek`（当前 `currentTime`）+ `playback`（pause/resume），以同步后端时间轴。旁观入口：`/?mode=observer`。

#### JSON 消息（服务端 → 客户端）

```json
// 注册应答：告知本连接是否为主连接
{ "type": "session_role", "role": "primary" | "observer" }

// 主连接切换广播（所有客户端）
{ "type": "primary_changed" }

// 语音播报开始（同步显示字幕，在 MP3 帧之前发送）
{
  "type": "tts",
  "utterance_id": 42,
  "channel": "fast" | "slow" | "user_answer" | "user",
  "text": "向左闪！",
  "video_time": 5.3,
  "playing": true
}

// TTS 被打断（USER_ANSWER 等），前端应立即 stop 当前 Audio
{ "type": "tts_interrupt", "utterance_id": 41 }

// 语音播报结束（队列空闲）
{ "type": "tts_end" }

// ASR 麦克风状态（驱动前端状态栏）
{ "type": "asr_state", "state": "listening" | "recording" | "processing" | "muted" }

// VLM 忙闲（驱动 dot-vlm）
{ "type": "vlm_state", "busy": true | false }

// 对话历史已清空
{ "type": "conversation_cleared" }

// 系统状态
{ "type": "status", "state": "started" | "video_ready", "duration": 120.5 }

// NitroGen 感知信号（调试面板）
{
  "type": "perception",
  "intent": "DODGE",
  "confidence": 0.87,
  "direction": "LEFT",
  "horizon": ["DODGE×6", "ATTACK×8", "NAVIGATE×2"],
  "video_time": 5.3
}

// seek 完成
{ "type": "seek_done", "time": 12.5 }

// 视频结束
{ "type": "video_ended" }
```

### 6.3 前端页面结构

```
┌─────────────────────────────────────────────────────────────┐
│  NitroGen 游戏语音教练                                         │
├──────────────────────────┬──────────────────────────────────┤
│                          │  💬 对话记录                        │
│   🎮 游戏视频              │                                   │
│   [HTML5 video player]   │  [AI-快] 向左闪！          5.3s   │
│                          │  [AI-慢] Boss右拳，左侧更安全 5.5s  │
│   ████████░░░░░  5.3s    │  [用户] 这段该怎么打？      12.1s  │
│                          │  [AI]   等右拳落地后打三下再撤       │
├──────────────────────────┤                                   │
│  🎤 持续收音中             │                                   │
│  ● NitroGen  ● VLM       │                                   │
│  ▶ TTS 播放中: "向左闪！"  │                                   │
└──────────────────────────┴───────────────────────────────────┘
```

麦克风图标状态说明：
- `🎤 持续收音中`：VAD 运行，等待用户说话
- `🎤● 检测到语音`：用户正在说话（VAD 触发）
- `🎤⊘ 暂停（TTS中）`：TTS 播报期间，ASR muted

---

## 7. 配置项（config.py）

```python
# backend/config.py

@dataclass
class Config:
    # NitroGen
    nitrogen_server:     str   = "tcp://localhost:5555"
    nitrogen_target_fps: float = 10.0      # 向 NitroGen 发送的帧率

    # 快系统
    cooldowns: dict = field(default_factory=lambda: {
        "sudden_dodge":      3.0,
        "attack_window":     4.0,
        "sustained_danger":  8.0,
        "movement_shift":   10.0,
        "pattern_completed": 5.0,
    })
    fast_trigger_confidence:  float = 0.75  # 快通道触发置信度阈值
    sustained_danger_sec:     float = 3.0   # DODGE 持续多久触发 SUSTAINED_DANGER

    # 慢系统
    vlm_model:            str   = "claude-sonnet-4-6"
    vlm_max_tokens:       int   = 120
    context_window_sec:   float = 15.0      # 上下文缓冲区时间窗口
    slow_max_queue_age:   float = 8.0       # 慢系统结果的有效期
    vlm_dedup_sec:        float = 5.0       # 同类事件 VLM 去重窗口（秒）

    # TTS
    tts_voice:               str   = "zh-CN-YunxiNeural"
    tts_rate:                str   = "+20%"
    tts_inter_utterance_gap: float = 0.8   # 两条语音之间的间隔
    tts_done_fallback_margin: float = 1.0  # 前端未回 tts_done 时的额外宽限
    tts_synthesis_timeout_sec: float = 15.0  # edge-tts 合成超时
    fast_hint_expire_sec:    float = 2.0   # 快提示超时丢弃

    # ASR
    whisper_model:     str = "base"
    whisper_language:  str = "zh"
    vad_silence_threshold: int   = 300    # 振幅静音阈值
    vad_speech_min_sec:    float = 0.5    # 最短有效语音
    vad_silence_end_sec:   float = 1.2    # 静音判定说话结束
    tts_mute_tail_sec:     float = 0.2    # TTS 结束后 ASR 额外静默

    # 被动提示频率上限（硬限制，防止任何单类事件刷屏）
    global_tts_min_interval: float = 2.0   # 任意两次被动播报之间至少间隔 2 秒
```

---

## 8. 依赖与环境

### 8.1 requirements.txt

```
# 核心
fastapi>=0.110.0
uvicorn[standard]>=0.27.0
websockets>=12.0

# NitroGen 通信
pyzmq>=25.0.0

# 视频处理
opencv-python>=4.9.0
pillow>=10.0.0

# AI
anthropic>=0.25.0
openai-whisper>=20231117

# TTS
edge-tts>=6.1.9

# 音频
pyaudio>=0.2.14
numpy>=1.26.0

# 工具
python-dotenv>=1.0.0
```

### 8.2 .env

```
ANTHROPIC_API_KEY=sk-ant-...
NITROGEN_SERVER=tcp://localhost:5555
```

### 8.3 NitroGen server 启动

```bash
# 在 GPU 机器上（Linux）
python scripts/serve.py /path/to/ng.pt --port 5555 --ctx 1

# 如果是本机，直接本地启动
# 如果是远程，修改 NITROGEN_SERVER=tcp://<remote_ip>:5555
```

---

## 9. 开发注意事项

### 9.1 NitroGen 相关

**帧率对齐**：视频原始帧率（30fps）和推理帧率（10fps）之间需要正确采样。不要对 NitroGen 发送重复帧（视频暂停时停止推理）。

**action 阈值**：buttons 输出是 float，阈值 0.5 是论文默认值，实际使用中根据游戏类型可能需要微调（动作游戏偏高，RPG 偏低）。

**chunk 对齐**：NitroGen 每次返回 16 帧的 chunk，但推理延迟约 200ms。`horizon_sequence` 的第 0 帧对应的游戏画面已经是过去时。感知信号提取时使用 `chunk[6..15]` 的统计（约对应推理完成后的当前时刻）。

**ZMQ 超时**：如果 NitroGen server 无响应，ZMQ 会永久阻塞。推理线程需要设置 `socket.RCVTIMEO = 2000`（2秒超时）并做重连逻辑。

### 9.2 快慢系统协调

**快慢通道内容不重叠**：慢系统 prompt 中需要传入 `last_fast_text`，让 VLM 避免重复刚才快通道已说的内容。

**PATTERN_COMPLETED 的时机**：NitroGen 判断"战斗结束"可能有 1-2 秒的惯性（因为 WAIT 需要连续出现才确认）。慢系统总结使用 `context_buffer` 的 15 秒窗口，覆盖完整的战斗过程。

**并行 VLM 调用限制**：同时最多允许 1 个 VLM 请求在途。如果上一个 VLM 请求还在处理中，新的 `trigger_slow` 事件加入等待队列（最多缓冲 1 个）。用户提问直接取消当前 VLM 请求，优先处理。

### 9.3 TTS 相关

**edge-tts 延迟**：edge-tts 需要网络请求，首次合成约 300-500ms。对于极短文本（≤4字）考虑预缓存常用提示词（"向左闪！"、"打！"、"注意！"）。

**音频打断**：TTS 打断需要在系统层面 kill 正在播放的音频进程，不同平台实现不同（Windows: `winsound` or `pygame.mixer.stop()`）。

**语速**：游戏场景建议语速调快 20-30%（edge-tts rate 参数 `+30%`），简短有力。

### 9.4 视频同步

**视频时间轴是真值**：所有事件的 `timestamp` 必须是视频时间（秒），不是系统时间。方便后续 debug 回放，也使 context_buffer 的时间窗口语义清晰。

**暂停处理**：用户暂停视频时，停止向 NitroGen 发送帧，清空 TTS 队列，允许用户自由提问。

**拖拽进度条**：seek 时清空 context_buffer（历史上下文失效），清空 TTS 队列，NitroGen client reset。

### 9.5 Demo 展示建议

**视频选择**：选择有明显节奏变化的 Boss 战片段（2-5分钟）。避免：全程高速战斗（触发太频繁）或全程走路（几乎没有触发）。

**调试面板**：开发阶段在右侧面板显示 NitroGen 实时感知信号（intent + confidence + horizon），方便验证过滤逻辑是否合理。

**首次运行**：NitroGen server 冷启动约需 10-30 秒加载模型。前端显示加载状态，视频就绪后才能开始分析。

---

## 10. 开发阶段划分

### Phase 1：基础管道（可独立验证）

- [x] VideoFramePipe：视频帧提取，帧率控制（已由前端 FrameBuffer 推帧替代）
- [x] NitroGenClient：ZMQ 通信，异步推理
- [x] parser.py：action vector → PerceptionSignal
- [x] 验证：打印每帧的感知信号，确认输出合理

### Phase 2：快系统

- [x] ActionFilter：突变检测，冷却机制
- [x] templates.py：事件 → 文本
- [x] TTSQueue + edge-tts：优先级队列，打断
- [x] 验证：视频播放时能听到稀疏的提示词

### Phase 3：慢系统

- [x] ContextBuffer：近期动作序列维护
- [x] VLMClient：Claude API + 图像输入
- [x] SlowTrigger：并行触发逻辑，结果入队
- [x] 验证：PATTERN_COMPLETED 后能听到有意义的总结

### Phase 4：用户交互

- [x] ASRHandler：Whisper + VAD，持续收音
- [x] TTSQueue ↔ ASRHandler 联动：播报时 mute，结束后 unmute
- [x] 用户提问流：识别完成 → USER_QUESTION 事件 → VLM → 最高优先级播报
- [x] 前端麦克风状态指示（收音中 / 检测到语音 / TTS中暂停）
- [x] 对话历史：多轮问答上下文

### Phase 5：前端

- [x] HTML 页面：视频播放器 + 对话面板
- [x] WebSocket 客户端（register 握手、自动重连、主连接 TTS）
- [x] 麦克风采集（AudioWorklet + 16kHz 重采样）
- [x] 上传视频文件流程

---

## 附录：关键数值参考

| 参数 | 值 | 来源 |
|------|----|------|
| NitroGen 推理帧率 | 10fps（建议） | 推理约200ms/chunk |
| action_horizon | 16 帧 | 论文 |
| 感知信号提取帧 | chunk[6..15] | 补偿推理延迟 |
| buttons 阈值 | 0.5 | 论文默认 |
| 快通道最长文本 | 8字 | TTS ≤200ms |
| 慢通道最长文本 | 40字 | 约5秒播放 |
| 全局最小播报间隔 | 2秒 | 认知负载限制 |
| VLM 最大并发 | 1 | 避免竞态 |
| context window | 15秒 | 覆盖一段完整操作 |
