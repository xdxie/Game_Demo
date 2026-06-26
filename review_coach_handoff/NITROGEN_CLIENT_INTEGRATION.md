# NitroGen 客户端集成说明

## 概述

`review_coach/nitrogen_client.py` 是 NitroGen 远端推理服务与 ReviewCoach 之间的适配层，负责：

1. 通过 SSH 隧道调用远端 GPU 上的 NitroGen FastAPI 服务
2. 收集 clip 内所有帧的推理结果
3. 直接转交 `ActionSequenceSummarizer` 生成结构化摘要
4. 摘要写入 `ReviewRequest`，喂给 `ReviewCoach`

---

## 链路结构

```
远端 autodl GPU
  └── NitroGen FastAPI (port 8000)
          │ SSH tunnel (localhost:8000 → remote:8000)
          ↓
NitroGenClient.predict_clip(frames, clip_start_sec, fps)
          │ ClipResult.raw  — list[dict]，每帧原始 JSON
          ↓
ClipResult.summarize()
  └── ActionSequenceSummarizer.from_nitrogen_frames()  → ActionSequenceInput
  └── ActionSequenceSummarizer().summarize()
          │ {action_summary, action_features, change_info}
          ↓
ReviewRequest.from_payload(payload)
          ↓
ReviewCoach().generate(request)
          │ ReviewResult
          ↓
SlowPath / TTS
```

---

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt   # 包含 requests
```

### 2. 确认远端服务在线

```bash
# 检查
ssh -p 18037 root@connect.bjb1.seetacloud.com \
  'grep -q "Application startup complete" /root/autodl-tmp/NitroGen/server.log && echo READY'

# 若未启动
ssh -p 18037 root@connect.bjb1.seetacloud.com \
  'cd /root/autodl-tmp/NitroGen && bash scripts/start_server.sh'
```

### 3. 调用示例

```python
from pathlib import Path
from review_coach import NitroGenClient, ReviewCoach, ReviewRequest

frame_paths = sorted(Path("frames/").glob("frame_*.jpg"))

with NitroGenClient() as client:          # 自动起/关 SSH 隧道
    clip = client.predict_clip(
        frames=frame_paths,
        clip_start_sec=4.0,               # 该 clip 在视频里的起始秒数
        fps=10.0,
    )

# clip.raw → list[dict]，可按需检查原始 JSON
print(clip)   # ClipResult(frames=40, t=[4.00s, 8.00s])

# 一步生成结构化摘要
summary = clip.summarize()
# {
#   "action_summary":  "玩家持续向右加速跑动，中途起跳一次",
#   "action_features": {"main_movement": "right", "jump_count": 1, ...},
#   "change_info":     {"is_change": True, "change_points": [...]}
# }

# 组装 ReviewRequest
payload = {
    "game_type":   "platformer",
    "game_name":   "New Super Mario Bros",
    "query":       "刚才那个跳台应该早点起跳吗？",
    "image_paths": ["frames/frame_0080.jpg"],
    "clip_start":  4.0,
    "clip_end":    8.0,
    **summary,
}
request = ReviewRequest.from_payload(payload)
result  = ReviewCoach().generate(request)
print(result["coaching_text"])
```

---

## API 参考

### `NitroGenClient`

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `base_url` | `str \| None` | `http://localhost:8000` | 推理服务地址 |
| `auto_tunnel` | `bool` | `True` | 是否自动管理 SSH 隧道 |
| `timeout` | `float` | `60.0` | 单帧请求超时（秒） |

| 方法 | 返回 | 说明 |
|---|---|---|
| `predict_frame(image)` | `dict` | 发单帧，返回服务端原始 JSON |
| `predict_clip(frames, clip_start_sec, fps, ...)` | `ClipResult` | 发整段 clip |
| `reset()` | `dict` | 清空服务端会话历史 |
| `info()` | `dict` | 查服务端状态 |

`image` 参数接受文件路径（`str` / `Path`）或原始图像 `bytes`（JPEG / PNG）。

### `ClipResult`

| 属性/方法 | 类型 | 说明 |
|---|---|---|
| `.raw` | `list[dict]` | 每帧的服务端原始 JSON |
| `.clip_start_sec` | `float` | clip 起始时间 |
| `.clip_end_sec` | `float` | clip 结束时间 |
| `.fps` | `float` | 采样帧率 |
| `.to_action_sequence()` | `ActionSequenceInput` | 转换为 Summarizer 输入格式 |
| `.summarize()` | `dict` | 直接运行 ActionSequenceSummarizer |

`summarize()` 返回结构：

```json
{
  "action_summary":  "玩家持续向右加速跑动，中途起跳一次",
  "action_features": {
    "main_movement":      "right",
    "jump_count":         1,
    "jump_segments":      [{"start_sec": 5.8, "end_sec": 6.2, "confidence": 0.83}],
    "direction_reversal": false,
    "dominant_pattern":   "run_right_then_jump",
    "risk_tags":          ["late_jump_possible"],
    "run_ratio":          0.72,
    "idle_ratio":         0.03,
    "duration_sec":       4.0
  },
  "change_info": {
    "is_change": true,
    "change_points": [
      {"timestamp_sec": 5.8, "from": "RIGHT", "to": "RIGHT+JUMP", "reason": "jump_started"}
    ]
  }
}
```

---

## 不使用 SSH 隧道的情况

已手动开好隧道，或服务公网可达时：

```python
# 方法 1：跳过隧道管理
client = NitroGenClient(auto_tunnel=False)
clip = client.predict_clip(frames, clip_start_sec=4.0)

# 方法 2：指定任意服务地址
client = NitroGenClient(base_url="http://192.168.1.100:8000", auto_tunnel=False)
```

手动起隧道：

```bash
ssh -p 18037 -L 8000:localhost:8000 -N root@connect.bjb1.seetacloud.com
```

---

## 文件位置

```
review_coach_handoff/
├── requirements.txt                      ← 包含 requests
└── review_coach/
    ├── __init__.py                       ← 导出 NitroGenClient, ClipResult
    ├── nitrogen_client.py                ← 本模块
    ├── action_sequence_summarizer.py     ← ClipResult.summarize() 调用此处
    ├── schemas.py                        ← ReviewRequest 接收 action_features
    └── review_coach.py                   ← 最终 generate()
```

远端推理服务的独立测试脚本仍保留在 `action_fast_system/run_inference.py`，与本模块无依赖关系。
