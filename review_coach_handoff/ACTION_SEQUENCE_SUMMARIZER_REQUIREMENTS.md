# Action Sequence Summarizer Requirements

## 背景

NitroGen 输出的是模型预判的逐帧动作序列，例如每一帧玩家是否在按 `LEFT`、`RIGHT`、`JUMP`、`RUN` 等。ReviewCoach slow/ 模块需要的不是完整逐帧序列，而是更短、更稳定、可解释的动作语义摘要。

因此需要一个中间层：`ActionSequenceSummarizer`。

一句话目标：

> 将 NitroGen 的逐帧动作预测序列压缩成 `action_summary`、`action_features` 和 `change_info`，供 slow/ 模块生成低延迟、可解释的教练文本。

## 为什么需要这层

逐帧动作序列只说明“玩家按了什么”，但 slow/ 还需要理解“这段操作大概是什么意图”。

例如原始序列：

```text
RIGHT, RIGHT, RIGHT, RIGHT+JUMP, RIGHT+JUMP, RIGHT, LEFT, IDLE
```

不建议直接传给 slow/。更推荐转成：

```json
{
  "action_summary": "玩家持续向右移动，在接近目标前跳跃，落地后短暂回拉方向",
  "action_features": {
    "main_movement": "right",
    "jump_count": 1,
    "jump_timing": "mid_sequence",
    "direction_reversal": true,
    "brief_idle": true
  }
}
```

这样 slow/ 的规则和 Gemini fallback 都能更快、更稳定地使用上下文。

## 职责边界

`ActionSequenceSummarizer` 负责：

- 聚合逐帧动作预测。
- 去掉重复帧和低置信噪声。
- 识别主要移动方向。
- 统计跳跃、冲刺、停顿、方向反转等动作特征。
- 生成短文本 `action_summary`。
- 生成结构化 `action_features`。
- 给出可选 `change_info`，说明动作变化点。

`ActionSequenceSummarizer` 不负责：

- 判断是否触发 slow/，这个仍由 `ActionFilter` / `EventDetector` 负责。
- 判断最终 TTS 怎么播，这个由 TTS queue 负责。
- 直接生成教练话术，这个由 ReviewCoach slow/ 负责。
- 单独判断“撞怪、掉坑、没顶到砖块”等视觉结果；这些需要事件检测或图像理解补充。

## 输入要求

建议输入字段：

```json
{
  "session_idx": 1,
  "clip_start_sec": 12.0,
  "clip_end_sec": 16.0,
  "fps": 30,
  "frames": [
    {
      "frame_idx": 360,
      "timestamp_sec": 12.0,
      "actions": {
        "LEFT": 0.01,
        "RIGHT": 0.94,
        "JUMP": 0.02,
        "RUN": 0.44
      }
    }
  ]
}
```

字段说明：

| Field | Required | Meaning |
|---|---:|---|
| `session_idx` | no | 当前片段或会话编号 |
| `clip_start_sec` | yes | 片段开始时间 |
| `clip_end_sec` | yes | 片段结束时间 |
| `fps` | no | 原视频或采样帧率 |
| `frames` | yes | 逐帧动作预测 |
| `frame_idx` | yes | 帧编号 |
| `timestamp_sec` | yes | 当前帧时间 |
| `actions` | yes | 动作到置信度的映射 |

## 输出要求

建议输出字段：

```json
{
  "action_summary": "玩家持续向右移动，在接近目标前跳跃，落地后短暂回拉方向",
  "action_features": {
    "duration_sec": 4.0,
    "main_movement": "right",
    "movement_segments": [
      {
        "action": "RIGHT",
        "start_sec": 12.0,
        "end_sec": 15.1,
        "confidence": 0.91
      }
    ],
    "jump_count": 1,
    "jump_segments": [
      {
        "start_sec": 13.2,
        "end_sec": 13.6,
        "confidence": 0.87
      }
    ],
    "run_ratio": 0.42,
    "idle_ratio": 0.12,
    "direction_reversal": true,
    "dominant_pattern": "run_right_then_jump",
    "risk_tags": ["early_jump_possible", "direction_correction_after_jump"]
  },
  "change_info": {
    "is_change": true,
    "change_points": [
      {
        "timestamp_sec": 13.2,
        "from": "RIGHT",
        "to": "RIGHT+JUMP",
        "reason": "jump_started"
      }
    ]
  }
}
```

## 输出字段说明

| Field | Required | Meaning |
|---|---:|---|
| `action_summary` | yes | 给 slow/ 和模型看的短文本摘要 |
| `action_features.duration_sec` | yes | 片段时长 |
| `action_features.main_movement` | yes | 主方向，`left` / `right` / `up` / `down` / `mixed` / `idle` |
| `movement_segments` | recommended | 连续移动片段 |
| `jump_count` | recommended | 跳跃次数 |
| `jump_segments` | recommended | 跳跃时间段 |
| `run_ratio` | optional | 冲刺/加速占比 |
| `idle_ratio` | optional | 停顿占比 |
| `direction_reversal` | recommended | 是否短时间反向 |
| `dominant_pattern` | recommended | 主操作模式 |
| `risk_tags` | recommended | 可疑风险标签 |
| `change_info` | recommended | 动作变化点 |

## 建议的 Risk Tags

平台跳跃游戏建议先支持这些标签：

| Tag | Meaning |
|---|---|
| `early_jump_possible` | 可能跳早 |
| `late_jump_possible` | 可能跳晚 |
| `rush_possible` | 持续前冲，可能太急 |
| `direction_correction_after_jump` | 跳后反向修正 |
| `hesitation` | 明显停顿或犹豫 |
| `repeated_jump` | 短时间重复跳 |
| `edge_or_gap_risk_possible` | 操作节奏像接近边缘或坑 |
| `enemy_timing_risk_possible` | 操作节奏像在处理敌人时机 |
| `reward_greedy_possible` | 操作节奏像为了奖励/金币改变路线 |

注意：这些标签是基于动作节奏的弱判断。是否真的有敌人、坑、金币、砖块，需要图像/事件检测提供证据。

## 传给 slow/ 的最终 payload

`ActionSequenceSummarizer` 输出后，可以和事件检测、截图一起合并传给 slow/：

```json
{
  "type": "trigger_slow",
  "trigger_slow": true,
  "game_type": "platformer",
  "game_name": "New Super Mario Bros.",
  "query": "",
  "action_summary": "玩家持续向右移动，在接近目标前跳跃，落地后短暂回拉方向",
  "action_features": {
    "main_movement": "right",
    "jump_count": 1,
    "direction_reversal": true,
    "dominant_pattern": "run_right_then_jump",
    "risk_tags": ["early_jump_possible", "direction_correction_after_jump"]
  },
  "change_info": {
    "is_change": true,
    "change_points": [
      {
        "timestamp_sec": 13.2,
        "from": "RIGHT",
        "to": "RIGHT+JUMP",
        "reason": "jump_started"
      }
    ]
  },
  "image_paths": ["current_frame.jpg"]
}
```

## 和 slow/ 的接口关系

slow/ 最重要消费：

- `type`
- `trigger_slow`
- `query`
- `action_summary`
- `action_features`
- `change_info`
- `image_paths`
- `game_type`
- `game_name`

其中：

- `type` / `trigger_slow` 决定是否进入慢通道。
- `action_summary` 是规则优先和 Gemini fallback 的核心上下文。
- `action_features` 用于更稳定地触发规则。
- `change_info` 用于解释关键动作变化点。
- `image_paths` 用于 Gemini fallback 判断视觉场景。

## 验收标准

最小可用版本需要满足：

- 能把连续相同动作压缩成片段。
- 能识别主移动方向。
- 能统计跳跃次数。
- 能识别短时间方向反转。
- 能输出不超过 80 字的中文 `action_summary`。
- 能输出稳定 JSON，不依赖自然语言解析。
- 对 3-5 秒片段的处理耗时应远低于 Gemini 调用耗时，目标为毫秒级。

推荐验收用例：

1. 连续向右跑，无跳跃。
2. 向右跑后跳跃。
3. 向右跳后短暂向左修正。
4. 多次短跳。
5. 长时间停顿后突然移动。
6. 动作置信度低、有噪声帧。

## 对接结论

NitroGen 的逐帧动作序列是有价值的上游输入，但它和 slow/ 需要的语义上下文不完全等价。建议由上游或独立 adapter 实现 `ActionSequenceSummarizer`，先把逐帧动作序列转成 `action_summary`、`action_features`、`change_info`，再交给 slow/ 生成最终教练文本。

