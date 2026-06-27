# 相对 merge-all 分支的改动说明

基准分支：`origin/merge-all`（HEAD `cafeeaf` UI overhaul）

本分支：`fast-vocab-and-combos`

---

## 1. 新增：游戏专属快路径词表（Game Vocab）

**文件：** `backend/fast/game_vocab.py`（新文件）

引入 `GameVocab` dataclass 和 `get_vocab(game_id)` 注册表，将快系统 TTS 文案从 `templates.py` 硬编码中抽离，按游戏切换词表。

每个 `GameVocab` 包含：

| 字段 | 用途 |
|---|---|
| `templates` | 4 个意图事件（闪避/攻击窗口/持续危险/走位）× 有/无方向，共 8 个 lambda |
| `button_to_text` | 单键按下 → TTS 文本（`BUTTON_PRESS` 事件） |
| `combo_to_text` | 两键组合 → TTS 文本（组合优先于单键） |
| `fallback` | 无模板时的兜底文本 |

已注册 5 个词表：

| game_id | 游戏 | 说明 |
|---|---|---|
| `_general` | 通用兜底 | 未注册 game_id 时使用 |
| `street_fighter_6` | 街头霸王 6 | 口语化按键/模板（戳一下、搓连段、往左挡等） |
| `black_myth_wukong` | 黑神话：悟空 | 完整 Xbox 按键映射 + 8 条法术/道具组合 |
| `new_super_mario_bros` | 新超级马里奥兄弟 | Wii/DS 按键映射（起跳、出招、旋转跳跃等） |
| `forza_horizon_5` | 极限竞速：地平线 5 | 驾驶操作（刹车、踩油门、转向等） |

---

## 2. 新增：按键边沿检测 + BUTTON_PRESS 事件

**文件：** `backend/fast/event.py`、`backend/fast/action_filter.py`

- 新增 `EventType.BUTTON_PRESS` 和 `GameEvent.button_name` 字段
- `ActionFilter._detect()` 末尾增加按键边沿检测（意图类事件优先，按键作兜底）
- 从 `pressed_buttons`（如 `["SOUTH(0.90)", "WEST(0.72)"]`）提取 conf ≥ 0.5 的按键名
- 冷却 1.2s，同帧多键新按时选置信度最高的

---

## 3. 新增：组合键识别（悟空法术/道具）

**文件：** `backend/fast/templates.py`

- 新增 `_parse_pressed()` / `_find_combo()` helper
- `render_fast()` 的 `BUTTON_PRESS` 分支优先级：**组合键 → 单键 → 空串**
- 组合 key 规则：两键名按字母序用 `+` 拼接（如 `RIGHT_TRIGGER+WEST`）

悟空 8 条组合：

| 组合 | 播报 |
|---|---|
| RT + X | 给我定！ |
| RT + Y | 聚形散气！ |
| RT + B | 广智救我！ |
| RT + A | 上吧孩儿们！ |
| LT + ↑ | 驱邪散！ |
| LT + ← | 避雷散！ |
| LT + → | 虎伏丹！ |
| LT + ↓ | 人参丸！ |

---

## 4. 重构：templates.py 代理到 game_vocab

**文件：** `backend/fast/templates.py`

- 删除原 SF6 硬编码文本，改为 `get_vocab(game_id)` 查表
- `render_fast(event, game_id)` 新增 `game_id` 参数
- 纯模板引擎，延迟 < 1ms，不调用 LLM

---

## 5. 集成：GameSession 携带 game_id

**文件：** `backend/main.py`

- `GameSession` 新增 `current_game_id` 字段（原 `current_game` 保留显示名）
- WebSocket `set_game` 消息读取 `game_id` 参数
- `render_fast(event, self.current_game_id)` 传入当前游戏
- **修复：** 空文本不再 `return` 整个 `_handle_event`，仅跳过 TTS，慢路径（VLM）继续触发
- **诊断日志：** `[模型原始]` / `[翻译]` / `[快提示]` 同时写 stdout 和 `session.log`

---

## 6. 前端：51 个游戏配 {id, label}，发送 game_id

**文件：** `frontend/app.js`

- `GAME_LIST` 从字符串数组改为 `{ id, label }` 对象数组（51 个游戏）
- 4 个目标游戏有专属词表，其余 47 个走 GENERAL 兜底
- `ws.onopen` 时立即发送当前选中游戏的 `game_id`，确保后端同步
- 切换游戏下拉框时发送 `{ type: "set_game", game, game_id }`

---

## 7. 新增：NitroGen 原始响应落盘

**文件：** `backend/nitrogen/fast_api_client.py`、`backend/nitrogen/factory.py`、`backend/config.py`

- 新增环境变量 `NITROGEN_DUMP_PATH`（落盘路径）和 `NITROGEN_DUMP_PRETTY`（是否缩进 JSON）
- 每次 `/predict` 响应追加写入 JSONL 文件，供离线分析
- `start()` / `stop()` 管理文件句柄

---

## 8. 调优：冷却与阈值

**文件：** `backend/config.py`、`backend/fast/action_filter.py`

| 参数 | 旧值 | 新值 | 原因 |
|---|---|---|---|
| `movement_shift` 冷却 | 3s | 15s | 减少「往后走位」频繁播报 |
| `move_magnitude` 阈值 | 0.5 | 0.7 | 过滤摇杆轻微抖动误触发 |

---

## 9. 新增工具脚本

| 文件 | 用途 |
|---|---|
| `tools/analyze_wukong_log.py` | 离线分析 `session.log` 中的按键/intent/播报统计 |
| `tools/dump_wukong_viewer.py` | 读取 NitroGen dump JSONL，输出摇杆/扳机/按键分布 |

---

## 10. 新增冒烟测试

**文件：** `smoke_test_vocab.py`

37 个检查，覆盖词表查找、按键播报、方向模板、ActionFilter 边沿检测、悟空组合键。运行：

```powershell
python smoke_test_vocab.py
```

---

## 不变部分

- 慢系统（VLM）逻辑、TTS/ASR 引擎、NitroGen 连接方式
- 事件检测的意图类规则（SUDDEN_DODGE / ATTACK_WINDOW 等）
- 前端 UI 布局、WebSocket 协议（除 `set_game` 新增 `game_id` 字段外）

---

## 使用方式

### 切换游戏词表

前端下拉框选游戏即可；后端自动按 `game_id` 切换词表。连接 WebSocket 后会立即同步当前选中游戏。

### 启用 NitroGen 原始数据落盘

在 `.env` 中设置：

```
NITROGEN_DUMP_PATH=wukong_actions.jsonl
NITROGEN_DUMP_PRETTY=0
```

重启后端，播放视频后可用 `tools/dump_wukong_viewer.py` 分析。

### 跑冒烟测试

```powershell
cd D:\Desktop\gamedemoclonev2
python smoke_test_vocab.py
```

---

## 已知限制

- **序列组合未做：** A→X 跳跃轻攻击、X 连按、长按 Y 蓄力等需要历史帧追踪，本轮未实现
- **其他 36 个游戏走 GENERAL：** 仅 SF6 / Wukong / Mario / Forza 有专属词表，其余使用通用兜底
- **RB+左摇杆=奔跑：** 摇杆方向不是按键边沿，归 MOVEMENT_SHIFT 处理，本轮未单独映射
