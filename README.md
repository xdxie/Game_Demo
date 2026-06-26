# NitroGen 游戏语音教练 Demo

基于 [NitroGen](https://github.com/MineDojo/NitroGen)（500M DiT 游戏 AI）+ Claude VLM 构建的实时游戏语音教练。
用户导入游戏视频，系统自动播放并分析，通过语音提供操作提示和策略建议，用户可随时开口提问。

> 详细架构设计见 [DESIGN.md](DESIGN.md)，团队分工见 [TEAM.md](TEAM.md)。

---

## 系统架构

```
NitroGen（快系统）  ─ 感知每帧画面 ─→ 动作过滤 ─→ 模板 ─→ TTS 播报（关键提示）
                                         │
                                         └──→ VLM（慢系统） ─→ TTS 播报（策略建议）
                                                    ↑
用户麦克风 ─ 持续收音 ─ VAD ─ Whisper ─────────────┘（用户提问，最高优先级）
```

- **快系统**：NitroGen 实时输出动作，经过三层过滤（突变检测 + 置信度门控 + 冷却时间）→ 自然语言模板 → TTS
- **慢系统**：关键事件或用户提问触发 Claude API → TTS
- **单 TTS 队列**：4 级优先级，USER_ANSWER > FAST_HINT > SLOW_ADVICE > SLOW_SUMMARY
- **持续收音**：无需按键，VAD 检测语音起止，TTS 播报期间自动 mute 防回声

---

## 目录结构

```
demo/
├── backend/
│   ├── main.py                  # FastAPI 主入口，WebSocket 服务，系统协调
│   ├── config.py                # 全局配置（所有可调参数集中于此）
│   ├── video/
│   │   ├── frame_buffer.py      # ★ 接收前端推帧，供 NitroGen 读取（Fix 11）
│   │   └── frame_pipe.py        # 备用：cv2 本地读帧（当前未被主流程使用）
│   ├── nitrogen/
│   │   ├── client.py            # ZMQ 客户端，异步推理循环
│   │   └── parser.py            # action chunk → PerceptionSignal
│   ├── fast/
│   │   ├── event.py             # GameEvent / EventType 数据结构
│   │   ├── action_filter.py     # 突变检测 + 冷却管理
│   │   └── templates.py         # 感知信号 → 提示文本
│   ├── slow/
│   │   ├── context_buffer.py    # ContextBuffer / ConversationHistory / FastHistory
│   │   ├── vlm_client.py        # Claude API 调用，Prompt 管理
│   │   └── trigger.py           # VLMRequestManager（生命周期、去重、取消）
│   ├── tts/
│   │   ├── engine.py            # edge-tts 封装，预缓存，MP3 bytes 发往前端（Fix 14）
│   │   └── queue.py             # 优先级队列，过期丢弃，ASR mute 联动
│   └── asr/
│       └── handler.py           # Whisper + 独立转写线程 + VAD（Fix 13）
├── frontend/
│   ├── index.html               # 页面结构
│   ├── style.css                # 暗色主题样式
│   └── app.js                   # WebSocket 客户端，麦克风采集，对话面板
├── scripts/
│   └── serve.py                 # NitroGen ZMQ 推理服务（GPU 机器上运行）
├── run.py                       # 快速启动脚本
├── requirements.txt             # 需额外安装的依赖
├── .env                         # API Key 和服务地址配置
├── DESIGN.md                    # 完整架构设计文档
└── TEAM.md                      # 7 人团队分工文档
```

---

## 环境准备

### 使用 live_vision conda 环境

该环境已包含 fastapi、uvicorn、numpy、pillow、pydub、python-dotenv 等基础依赖。
需要额外安装：

```bash
conda activate live_vision
cd demo
pip install -r requirements.txt
```

requirements.txt 中额外安装的包：

| 包 | 版本 | 用途 |
|----|------|------|
| pyzmq | 27.1.0 | NitroGen ZMQ 通信 |
| opencv-python | 4.13.0 | 视频处理工具（frame_pipe.py 备用） |
| anthropic | 0.112.0 | Claude VLM API |
| openai-whisper | 20250625 | 本地语音识别 |
| edge-tts | 7.2.8 | TTS 合成（需联网） |
| pygame | 2.6.1 | ~~音频播放~~ 已不再用于 TTS 播放（Fix 14） |
| aiofiles | 25.1.0 | 异步文件操作 |

### 配置 .env

```
ANTHROPIC_API_KEY=sk-ant-xxxxx   # 必填
NITROGEN_SERVER=tcp://localhost:5555   # NitroGen 服务地址，远程改为 tcp://<ip>:5555
```

---

## 启动方式

### Step 1：启动 NitroGen 推理服务（GPU 机器，Linux）

```bash
# 在安装了 NitroGen 的 GPU 环境中
python scripts/serve.py /path/to/nitrogen.pt --port 5555 --ctx 1
```

> 如果 NitroGen 在远程机器上，修改 `.env` 中 `NITROGEN_SERVER=tcp://<remote_ip>:5555`

### Step 2：启动后端服务（本机，Windows）

```bash
conda activate live_vision
cd demo
python run.py
```

等价命令：`uvicorn backend.main:app --host 0.0.0.0 --port 8000`

### Step 3：打开前端页面

浏览器访问：`http://localhost:8000`

1. 点击"选择视频"，选择本地游戏视频文件（MP4/AVI/MKV）
2. 点击"▶ 开始分析"（无需传路径给后端，前端直接推帧）
3. 浏览器请求麦克风权限，允许后系统开始持续收音
4. 视频播放时，AI 语音提示会通过**浏览器扬声器**自动播报
5. 随时开口说话即可提问

**旁观模式**：主页面开始分析后，访问 `http://localhost:8000/?mode=observer` 可只读查看对话与调试信号（不推帧、不收音）。

---

## 已实现功能清单

### 核心架构（全部完成）

- [x] **FrameBuffer**（Fix 11）：接收前端 canvas 推帧（10fps JPEG + 视频时间戳），供 NitroGen 读取；与 VideoFramePipe 接口兼容，NitroGenClient 无需修改；同时解决了文件路径传递问题
- [x] **NitroGenClient**：ZMQ REQ/REP 通信，异步推理循环，2 秒超时自动重连
- [x] **PerceptionSignal 解析**：`j_left/j_right/buttons` → 主导意图/置信度/移动方向/预测序列；使用 `chunk[6..15]` 补偿 200ms 推理延迟
- [x] **ActionFilter**：5 类事件检测（SUDDEN_DODGE / ATTACK_WINDOW / SUSTAINED_DANGER / MOVEMENT_SHIFT / PATTERN_COMPLETED），三层过滤（突变检测 + 置信度 + 冷却时间）
- [x] **快通道模板引擎**：事件 → 短提示文本（≤8字），有方向/无方向双模板
- [x] **ContextBuffer**：15 秒滚动窗口，run-length 压缩意图序列，关键事件追踪
- [x] **ConversationHistory**：多轮问答历史（仅 USER_QUESTION 写入），最多 5 轮，供追问使用
- [x] **FastHistory**：近期快通道播报记录（10 秒有效），避免慢通道内容重复
- [x] **VLM 客户端**：Claude API 异步调用，图像 + 上下文 + 感知信号组合 prompt
- [x] **VLMRequestManager**：单 in-flight + 单 pending 管理，USER_QUESTION 取消当前请求，同类事件 5 秒去重
- [x] **TTSEngine**（Fix 14）：edge-tts 合成 → MP3 bytes 通过 WebSocket 发往前端播放；用 pydub 精确估算播放时长触发 on_complete；不再依赖 pygame
- [x] **TTSQueue**：4 级优先级堆，过期自动丢弃，USER_ANSWER 打断，与 ASRHandler mute/unmute 联动
- [x] **ASRHandler**（Fix 13）：独立转写线程 + Queue，`_flush()` 非阻塞，VAD 期间 Whisper 可并发运行；振幅 VAD，TTS 期间暂停避免回声
- [x] **FastAPI 主入口**：`/start`、`/stop` HTTP API，`/ws` WebSocket，GameSession 全系统协调，视频 seek 全状态重置
- [x] **前端**（Fix 11、14）：canvas 帧捕获（10fps → WebSocket 二进制）；TTS 音频接收并用 Audio API 播放；麦克风 PCM 采集；对话面板；调试面板

### 配置（全部完成）

- [x] 所有可调参数集中在 `backend/config.py`，每个参数标注了负责调优的角色编号

---

## 遗留问题与 TODO

### ✅ 已解决的架构问题

| 原编号 | 问题 | 解决方式 |
|--------|------|---------|
| 11 | VideoFramePipe 与视频播放不真正同步 | 前端 canvas 10fps 截帧 → WS 二进制推送，后端 FrameBuffer 接收，彻底消除累积误差 |
| 13 | Whisper 阻塞 ASR 线程 | 独立转写线程 + Queue，`_flush()` 非阻塞，VAD 与 Whisper 并发运行 |
| 14 | TTS 只在服务端播放 | TTSEngine 合成后通过 WS `send_bytes` 发 MP3，前端 Audio API 播放，移除 pygame 依赖 |
| 2  | 前端文件路径无法传给后端 | Fix 11 已使后端不再需要视频文件路径，此问题自然消除 |
| 3  | edge-tts 与 pygame 的 asyncio 嵌套风险 | Fix 14 移除了 pygame，TTS 播放路径不再经过 pygame |

---

### 🔴 关键阻塞项（必须解决才能运行）

**1. NitroGen 推理服务未验证**

`scripts/serve.py` 是基于 NitroGen 上游接口假设编写的适配包装，实际导入路径（`from nitrogen.model import NitroGenModel`）需要在真实 NitroGen 安装环境中验证。

- 负责人：**1 号**
- 具体工作：在 GPU 机器上跑通 `scripts/serve.py`，验证 ZMQ 请求/响应格式与 `backend/nitrogen/client.py` 的假设一致
- 关键不确定点：NitroGen 的 `response["pred"]` 字典键名是否是 `j_left/j_right/buttons`，shape 是否为 `(16,2)/(16,2)/(16,21)`

---

### 🟡 需要人工调优的模块

**2. 动作解析阈值（2 号）**

`backend/nitrogen/parser.py` 中的意图推断逻辑完全基于设计假设，核心问题：

- `ATTACK_BUTTONS / DODGE_BUTTONS` 的分组是否与游戏实际操作语义对应（不同游戏差异极大）
- `_group_score()` 使用 `max(axis=1).mean()` 可能不是最优聚合方式
- `NAVIGATE` 的分数用 `joystick_mag * 0.5` 与其他按键分数不在同一量纲
- 需要在 1 号提供真实数据后重新标定

**3. 动作过滤阈值（2 号）**

`backend/fast/action_filter.py` 中所有数值均为估算：

```python
confidence_threshold = 0.75   # 未经实测
sustained_danger_sec = 3.0    # 未经实测
COOLDOWNS = { SUDDEN_DODGE: 3.0, ATTACK_WINDOW: 4.0, ... }  # 全部未经实测
```

在真实游戏视频上运行前，`primary_intent` 的置信度分布未知，`0.75` 可能过高（几乎不触发）或过低（触发太频繁）。

**4. VLM Prompt 质量（4 号）**

`backend/slow/vlm_client.py` 中的 `SYSTEM_PROMPT` 和 user message 构造是初版，尚未经过真实游戏帧测试：

- 回答是否会过长 / 过于泛化 / 忽略 NitroGen 信号
- 不同触发场景（策略类、状态类、评价类、PATTERN_COMPLETED 总结）的 prompt 分支是否合理
- `vlm_max_tokens=120` 对应约 40 字，是否在此限制下仍能给出有意义的回答

**5. TTS 音色与语速（3 号）**

`backend/config.py` 默认值：`tts_voice = "zh-CN-YunxiNeural"`, `tts_rate = "+20%"`

- 这是初始猜测值，需在真实游戏场景下听感评估
- 可用中文声音列表：`python -m edge_tts --list-voices | findstr zh-CN`

**6. VAD 参数（5 号）**

`backend/asr/handler.py` 中的 VAD 参数对麦克风环境高度敏感：

```python
SILENCE_THRESHOLD = 300   # 需在真实环境（含游戏背景音）下校准
SILENCE_END_SEC   = 1.2   # 说话停顿多长判定为结束
TTS_MUTE_TAIL_SEC = 0.2   # TTS 结束后额外静默，消除回声尾音
```

游戏背景音可能导致 VAD 持续误触发，需要测试并可能改为能量差分或更复杂的 VAD（如 webrtcvad）。

---

### 🟢 前端功能待完善（6 号）

**7. 前端 UI 深度优化**

当前前端是功能性骨架，以下部分待 6 号完善：

- [ ] 对话气泡的动效与视觉层次（快通道/慢通道/用户/AI 四种样式已有，可深化）
- [ ] 麦克风状态指示动效（当前仅文字，建议做成波形动画）
- [ ] TTS 正在播报时对应气泡的"高亮"或"播放中"状态
- [ ] 视频进度条与对话时间戳的联动（点击对话气泡跳到对应视频时刻）
- [ ] 视频区和对话区的比例可调（拖动分割线）
- [ ] 拖动进度条时的加载中状态（seek 期间的过渡体验）

**8. 浏览器安全上下文限制**

麦克风权限和 `canvas.toBlob()` 均需要 HTTPS 或 localhost。目前 localhost 场景正常，若部署到内网其他机器访问，需要配置 HTTPS（如 nginx 反代 + 自签名证书）。

---

### 🔵 架构层面的已知局限

**9. 单用户 Demo 架构**

`backend/main.py` 中 `_session` 是全局变量，只支持单个会话。多人同时访问会互相覆盖。Demo 场景够用，正式部署需要会话隔离。

**10. TTS 播放完成时序**

播放完成以**前端 `tts_done`（带 `utterance_id`）为主路径**，后端 fallback 定时器（估算时长 + `tts_done_fallback_margin`）兜底。MP3 通过 `0x03 + utterance_id` 二进制帧与字幕关联，消除 JSON/MP3 乱序问题。

---

## 各角色当前最优先任务

| 角色 | 最优先任务 | 关键文件 |
|------|-----------|---------|
| 1 号 | 跑通 `scripts/serve.py`，验证 ZMQ 响应格式，打印真实 `j_left/buttons` 数值分布 | `scripts/serve.py`, `backend/nitrogen/client.py` |
| 2 号 | 等 1 号数据后，校准 `parser.py` 意图推断，调整 `action_filter.py` 阈值 | `backend/nitrogen/parser.py`, `backend/fast/action_filter.py` |
| 3 号 | 选音色（`tts_voice`），调语速（`tts_rate`），逐条朗读快通道模板文本 | `backend/tts/engine.py`, `backend/config.py`, `backend/fast/templates.py` |
| 4 号 | 用真实游戏帧 + 感知信号调用 Claude，评估初版 prompt，迭代 | `backend/slow/vlm_client.py` |
| 5 号 | 在真实麦克风环境下测 VAD 参数，测 mute 防回声效果 | `backend/asr/handler.py`, `backend/config.py` |
| 6 号 | 深化前端 UI（文件路径问题已不存在，可直接开始 UI 优化） | `frontend/app.js`, `frontend/style.css` |
| 7 号 | 选视频（可立即开始），等各模块就绪后做端到端集成联调 | `backend/main.py`，Demo 视频 |
