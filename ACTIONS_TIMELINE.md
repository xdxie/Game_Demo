# 关键动作时间线 JSON（v1）

视频抽帧 → NitroGen 快系统（**mock** / **fast_api** / zmq）→ 后处理过滤 → 供 VLM 使用的 JSON。

## 生成时机

1. 用户选择视频 → `loadedmetadata`
2. 前端每 **2s** 抽一帧（最多 90 帧）→ `POST /actions/ingest-batch`
3. 后端过滤后写入 `_action_timeline`，`GET /actions/timeline` 可查看

## 格式

```json
{
  "version": 1,
  "source": "mock_nitrogen",
  "duration_sec": 120.0,
  "sample_interval_sec": 2.0,
  "key_actions": [
    {
      "t_sec": 4.0,
      "steer": -0.75,
      "throttle": 1,
      "brake": 0,
      "intent": "NAVIGATE",
      "confidence": 0.8,
      "label": "left_throttle"
    }
  ]
}
```

| 字段 | 说明 |
|------|------|
| `source` | `mock_nitrogen` / `nitrogen_fast_api` / `nitrogen_zmq` |
| `steer` | [-1, 1] 左摇杆 X（通用手柄语义，非驾驶专用） |
| `throttle` | 0/1，常映射右扳机/确认键 |
| `brake` | 0/1，常映射左扳机/防御键 |
| `intent` | 派生意图，供快系统兼容 |
| `label` | 简短标签或 `hint_text` 摘要 |

## VLM 使用方式

提问或慢事件触发 VLM 时，将当前时间 ±20s 窗口内的 `key_actions` 摘要拼入 prompt（见 `ActionTimeline.summary_near`）。

## 接实机 NitroGen

**推荐（action_fast_system）**：`.env` 设 `NITROGEN_MOCK=0`、`NITROGEN_BACKEND=fast_api`，按 [action_fast_system/README.md](action_fast_system/README.md) 建立 SSH 隧道后启动陪玩。

**旧路径（ZMQ）**：`NITROGEN_BACKEND=zmq` + `NITROGEN_SERVER=tcp://...`

JSON 字段保持不变，仅 `source` 与 `label` 内容随后端变化。
