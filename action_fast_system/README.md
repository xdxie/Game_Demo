# NitroGen 远程推理客户端 (`run_inference.py`)

一个本地脚本：连续把一段视频帧发送到远端 autodl GPU 机器上常驻的 NitroGen FastAPI 推理服务，每帧得到一个 JSON 结果，并统计每帧耗时。

## 工作原理

```
┌────────────────────┐     SSH tunnel      ┌────────────────────────────┐
│ 本地 (Windows)      │  localhost:8000  ─► │ 远端 autodl GPU             │
│ run_inference.py    │      :18037         │ /root/autodl-tmp/NitroGen   │
│                     │                     │ action_change_server.py     │
│  POST /reset        │                     │  常驻 FastAPI :8000          │
│  POST /predict (x4) │                     │  GET /info /reset /predict  │
└────────────────────┘                     └────────────────────────────┘
```

调用流程：
1. 启动一条 SSH 端口转发：把本地 `127.0.0.1:8000` 映射到远端的 `localhost:8000`（脚本自动起，退出时自动关）。
2. `POST /reset` 让服务端清空帧历史，开启一段新会话。
3. 顺序遍历 `inputs/frame_*.jpg`，每帧 `POST /predict`，服务端返回 JSON。
4. 每帧落盘到 `outputs/<frame_stem>.json`，并用 `time.perf_counter()` 测端到端耗时。
5. 全部跑完后打印每帧表格 + 汇总 (mean/median/min/max/stdev)，并写入 `outputs/_timings.json`。

## 依赖

| 项 | 说明 |
|---|---|
| Python | 3.8+ |
| pip 包 | `requests`（已带 `2.32.x`） |
| 系统命令 | `ssh`（OpenSSH，Windows 10+ 自带） |
| 远端服务 | 必须已经跑起来（见下节） |

```bash
pip install requests
```

## 远端服务的状态管理

脚本本身**不会**去启动远端服务，只会连接。日常只需要确认它在运行：

```bash
# 检查
ssh -p 18037 root@connect.bjb1.seetacloud.com 'cd /root/autodl-tmp/NitroGen && \
  if [ -f .server.pid ] && kill -0 $(cat .server.pid) 2>/dev/null; then \
    echo "Server running, PID=$(cat .server.pid)"; \
  else echo "Server NOT running"; fi'

# 若没在跑，启动它（约 15-30s 后才 ready）
ssh -p 18037 root@connect.bjb1.seetacloud.com 'cd /root/autodl-tmp/NitroGen && bash scripts/start_server.sh'

# ready 检查
ssh -p 18037 root@connect.bjb1.seetacloud.com 'cd /root/autodl-tmp/NitroGen && \
  grep -q "Application startup complete" server.log && echo READY'
```

SSH 凭据：

| 字段 | 值 |
|---|---|
| host | `connect.bjb1.seetacloud.com` |
| port | `18037` |
| user | `root` |

## 目录约定

```
test/
├── run_inference.py        ← 本脚本
├── inputs/                 ← 放待推理的帧
│   ├── frame_0039.jpg
│   ├── frame_0040.jpg
│   ├── frame_0041.jpg
│   └── frame_0042.jpg
└── outputs/                ← 脚本自动创建
    ├── frame_0039.json     ← 每帧的推理结果
    ├── frame_0040.json
    ├── frame_0041.json
    ├── frame_0042.json
    └── _timings.json       ← 耗时汇总
```

## 快速开始

```bash
# 1. 把帧放到 inputs/，文件名按字典序就是发送顺序
# 2. 一行起飞：
python run_inference.py
```

输出示例：

```
[scan] found 4 frame(s) in inputs:
       - frame_0039.jpg
       - frame_0040.jpg
       - frame_0041.jpg
       - frame_0042.jpg
[tunnel] up on localhost:8000
[server] /reset OK -> {"status":"ok"}

[infer] sending frames continuously...
  [ 0] frame_0039.jpg  time=  368.2 ms  frame_idx=0  is_change=False  -> outputs\frame_0039.json
  [ 1] frame_0040.jpg  time=  368.0 ms  frame_idx=1  is_change=False  -> outputs\frame_0040.json
  [ 2] frame_0041.jpg  time=  329.4 ms  frame_idx=2  is_change=False  -> outputs\frame_0041.json
  [ 3] frame_0042.jpg  time=  342.3 ms  frame_idx=3  is_change=False  -> outputs\frame_0042.json

[summary] per-frame latency (s):
  frame_0039.jpg          368.15 ms
  frame_0040.jpg          367.98 ms
  frame_0041.jpg          329.41 ms
  frame_0042.jpg          342.30 ms

  frames : 4
  total  :  1407.84 ms
  mean   :   351.96 ms
  median :   355.14 ms
  min    :   329.41 ms
  max    :   368.15 ms
  stdev  :    19.33 ms
  (first frame often slowest due to warmup)

[saved] timings -> outputs\_timings.json
```

## 命令行参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--input-dir` | `inputs` | 帧所在目录 |
| `--output-dir` | `outputs` | JSON 写出目录（自动创建） |
| `--pattern` | `frame_*.jpg` | 帧文件名 glob，按文件名排序后发送 |
| `--server` | `http://localhost:8000` | 推理服务地址（默认通过 SSH 隧道） |
| `--no-tunnel` | off | 不起 SSH 隧道（已自己开好 / 服务公网可达时用） |
| `--no-reset` | off | 跳过开头的 `/reset`，接续上一次会话 |

### 常用场景

```bash
# 默认：inputs/ -> outputs/
python run_inference.py

# 换目录
python run_inference.py --input-dir foo --output-dir bar

# 处理 PNG
python run_inference.py --pattern "*.png"

# 自己开好隧道了，不想让脚本再开
python run_inference.py --no-tunnel

# 接着上一次的会话送帧（不清服务端历史）
python run_inference.py --no-reset
```

### 手动起 SSH 隧道（可选）

```bash
ssh -p 18037 -L 8000:localhost:8000 -N -f root@connect.bjb1.seetacloud.com
# 关闭
# Linux / macOS:  pkill -f 'ssh.*18037.*-L 8000'
# Windows PowerShell: Get-Process ssh | Stop-Process
```

## 输出 JSON 字段

每帧落盘的 `outputs/<frame>.json` 由服务端返回，关键字段：

| 字段 | 含义 |
|---|---|
| `frame_idx` | 在本次会话里的帧序号（从 0 开始） |
| `session_idx` | 服务端总会话计数 |
| `auto_reset` | 服务端是否触发了自动 reset |
| `action_summary` | 模型预测的手柄动作摘要（左右摇杆均值/方差、按键、扳机） |
| `is_change` | 是否检测到动作变化 |
| `change_info` | `mode` / `distance` / `threshold` / `history_size_used` |

汇总文件 `outputs/_timings.json`：

```jsonc
{
  "frames": [
    { "file": "frame_0039.jpg", "elapsed_sec": 0.368, "frame_idx": 0, "is_change": false },
    ...
  ],
  "total_sec":  1.408,
  "mean_sec":   0.352,
  "median_sec": 0.355,
  "min_sec":    0.329,
  "max_sec":    0.368
}
```

## 故障排查

| 现象 | 原因 / 处理 |
|---|---|
| `SSH tunnel did not come up on localhost:8000 within 15s` | SSH 凭据 / 网络问题，手动 `ssh -p 18037 root@connect.bjb1.seetacloud.com` 试一下 |
| `requests.exceptions.ConnectionError` | 隧道挂了或服务没起，按"远端服务的状态管理"章节排查 |
| 第一帧明显比后面慢 | 正常，模型 lazy warmup；如需排除可先送一张哑帧再开始计时 |
| `is_change` 始终 False | 帧太少 / 动作未变化，检查 `change_info.history_size_used` 是否还在累积 |
| 想看服务端日志 | `ssh -p 18037 root@connect.bjb1.seetacloud.com 'tail -f /root/autodl-tmp/NitroGen/server.log'` |

## 耗时口径说明

脚本测的是**端到端单帧延迟**（client 视角）：

```
t = 上传 multipart  +  网络 RTT  +  服务端模型推理  +  返回 JSON
```

不是纯 GPU 推理时间。若要拆解：
- 服务端真实推理耗时见 `server.log` 里的相关日志。
- 网络部分主要由 SSH 隧道 RTT 决定，跨地域调用时通常是大头。
