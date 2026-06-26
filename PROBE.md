# E2E 链路探针使用说明

浏览器探针用于**在不打开游戏视频、不授权麦克风**的情况下，自动验证前后端关键链路是否通畅。

探针页面地址：**http://localhost:8000/probe**

---

## 探针验证什么

探针按顺序执行 10 项检查，覆盖整条数据通路：

```
HTTP 健康检查
  → 启动/附着分析会话
  → WebSocket 连接 + register(player) 成为主连接
  → video_ready 元数据
  → 推送合成视频帧 (0x02)
  → 推送静音 PCM (0x01)
  → TTS 合成 → MP3 二进制 (0x03) → tts_done 回传
  → 旁观连接 register(observer)
  → （可选）等待 NitroGen perception JSON
```

**适合场景：**

- 刚部署完环境，快速确认服务能跑通
- 改了 WebSocket / TTS 协议后做回归
- CI 里用 Playwright 做冒烟测试

**不适合场景：**

- 验证 VLM 回答质量、ASR 识别准确率
- 验证 NitroGen 模型效果（感知步骤仅为连通性参考）

---

## 手动使用（推荐入门）

### 1. 启动后端

在项目根目录执行：

```bash
python run.py
```

服务默认监听 `http://localhost:8000`。

### 2. 打开探针页

任选一种入口：

| 方式 | 地址 |
|------|------|
| 直接访问 | http://localhost:8000/probe |
| 从主应用 | http://localhost:8000 → 右上角点击 **「探针」** |

### 3. 运行探针

点击 **「▶ 运行全部探针」**。

页面会实时显示：

- **顶部摘要**：尚未运行 / 运行中 / 全部通过 / 有警告 / 失败
- **探针步骤**：每项的状态图标（○ 待运行、◉ 运行中、✓ 通过、! 警告、✗ 失败）和耗时
- **运行日志**：带时间戳的详细输出

### 4. 解读结果

| 摘要 | 含义 |
|------|------|
| 绿色「全部通过」 | 所有关键步骤成功，链路正常 |
| 黄色「通过（N 项警告）」 | 关键步骤成功，部分非关键步骤失败（常见：NitroGen 未启动） |
| 红色「失败 N 项关键探针」 | 有关键步骤失败，需要排查 |

**关键步骤**失败时，探针会**自动中止**后续检查，避免一连串连锁误报。

### 5. 结束后清理（可选）

- 若探针**新建了会话**（`/start` 返回 200），可点击 **「停止探针会话」** 调用 `POST /stop`。
- 若会话**本来就在运行**（`/start` 返回 409），探针不会销毁现有会话，无需额外操作。

---

## 各步骤说明

| # | 步骤 ID | 说明 | 是否关键 |
|---|---------|------|----------|
| 1 | `health` | `GET /probe/health` 服务端快照 | 是 |
| 2 | `status` | `GET /session/status` 会话状态 | 是 |
| 3 | `start` | `POST /start` 启动分析（已运行则接受 409） | 是 |
| 4 | `ws-register` | WebSocket 连接 + `register: player` → `session_role: primary` | 是 |
| 5 | `video-ready` | 发送 `video_ready`，等待服务端 `status` 回显 | 是 |
| 6 | `push-frame` | Canvas 生成 256×256 JPEG，按 `0x02` 协议推送 | 是 |
| 7 | `push-pcm` | 推送 100ms 静音 PCM (`0x01`) | 否 |
| 8 | `tts-roundtrip` | `POST /probe/tts-echo` → 收 JSON `tts` → 收 MP3 `0x03` → 发 `tts_done` | 是 |
| 9 | `observer` | 第二条 WS 以 `observer` 注册 | 否 |
| 10 | `perception` | 连推 5 帧，等待 `perception` JSON（mock 或实机） | 是 |

### 常见失败原因

| 失败步骤 | 可能原因 |
|----------|----------|
| `health` / `status` | 后端未启动或端口不对 |
| `start` | GameSession 初始化异常（依赖缺失、配置错误） |
| `ws-register` | 防火墙、反向代理未配置 WebSocket |
| `tts-roundtrip` | edge-tts 无法访问外网、合成超时；或探针在 POST 之后才监听 WS 导致丢消息（已修复） |
| `perception` | live 模式下 NitroGen 未启动；mock 模式下检查是否已推帧 |

---

## 仅测前端（无 NitroGen GPU，推荐入门）

默认已开启 **NitroGen 模拟模式**，无需 ZMQ 服务即可 **10 步探针全绿**：

1. `pip install -r requirements.txt`
2. `python run.py`（终端应出现 `NitroGen: mock 模式`）
3. 打开 http://localhost:8000/probe → **运行全部探针**

`GET /probe/health` 会返回 `"nitrogen_mode": "mock"`。感知步骤收到的是后端循环推送的演示 `perception` JSON，用于验证前端调试面板与 WebSocket 通路。

接上真实 NitroGen 时，在 `.env` 设置：

**推荐（action_fast_system HTTP）：**

```
NITROGEN_MOCK=0
NITROGEN_BACKEND=fast_api
NITROGEN_FAST_API_URL=http://localhost:8000
```

先按 [action_fast_system/README.md](action_fast_system/README.md) 建立 SSH 隧道。

**旧路径（ZMQ）：**

```
NITROGEN_MOCK=0
NITROGEN_BACKEND=zmq
NITROGEN_SERVER=tcp://localhost:5555
```

---

## 环境要求

### 最低要求（验证 HTTP + WS + 推帧 + TTS + perception mock）

- Python 依赖已安装：`pip install -r requirements.txt`
- 能执行 `python run.py` 且无报错
- 本机可访问 **edge-tts**（TTS 步骤需要外网）
- **不需要** NitroGen GPU 服务（默认 mock）

### 完整通过（实机 NitroGen 感知）

除上述外，还需：

1. `.env` 中 `NITROGEN_MOCK=0`
2. **推荐** `NITROGEN_BACKEND=fast_api`，按 `action_fast_system/README.md` 起 SSH 隧道并确认远端 `/predict` 可达
3. 或 **旧路径** ZMQ：启动 `scripts/serve.py` 并设 `NITROGEN_BACKEND=zmq`、`NITROGEN_SERVER=tcp://localhost:5555`

### 不需要

- 选择本地视频文件
- 浏览器麦克风权限
- 在主页面点击「开始分析」（探针会自行 `POST /start`）

---

## 与主应用的关系

| 项目 | 说明 |
|------|------|
| 会话启动 | 探针自动 `POST /start`；若已有会话返回 409 并继续测试 |
| 主连接占用 | 探针会占用一个 `player` 主 WebSocket |
| 并行使用 | 探针运行期间，避免再开主页面点「开始分析」，以免争抢主连接 |
| 旁观模式 | 探针与 `/?mode=observer` 独立；探针自己也会测 observer 注册 |

---

## 自动化使用（CI / Playwright）

### 自动运行

访问带参数的 URL，页面加载后约 300ms 自动开始：

```
http://localhost:8000/probe?autorun=1
```

### 读取结果

探针结束后，结果写入全局对象：

```javascript
window.__PROBE_RESULT__
```

示例结构：

```json
{
  "ok": true,
  "passed": 8,
  "warned": 1,
  "failed": 0,
  "steps": [
    { "id": "health", "status": "pass", "detail": "session=false, ws=0", "ms": 42, "critical": true },
    { "id": "perception", "status": "warn", "detail": "NitroGen 未回传 perception...", "ms": 8102, "critical": false }
  ],
  "ts": "2026-06-25T12:00:00.000Z"
}
```

字段说明：

| 字段 | 含义 |
|------|------|
| `ok` | `true` 表示无关键失败（可有警告） |
| `passed` / `warned` / `failed` | 各状态步骤数量 |
| `steps[].status` | `pass` / `warn` / `fail` |
| `steps[].critical` | 是否为关键步骤 |

同时会派发 DOM 事件：

```javascript
window.addEventListener('probe-complete', (e) => {
  console.log(e.detail);  // 与 __PROBE_RESULT__ 相同
});
```

### Playwright 示例

```javascript
import { test, expect } from '@playwright/test';

test('E2E probe smoke', async ({ page }) => {
  await page.goto('http://localhost:8000/probe?autorun=1');

  await page.waitForFunction(
    () => window.__PROBE_RESULT__ != null,
    { timeout: 90_000 },
  );

  const result = await page.evaluate(() => window.__PROBE_RESULT__);

  expect(result.failed, JSON.stringify(result.steps, null, 2)).toBe(0);
  expect(result.ok).toBe(true);
});
```

建议超时设为 **90 秒** 以上（TTS 合成可能较慢）。

---

## HTTP API 参考

探针页面之外，也可直接调用以下接口：

### `GET /probe`

返回探针 HTML 页面。

### `GET /probe/health`

服务端组件快照，不启动会话。

```bash
curl -s http://localhost:8000/probe/health | jq
```

响应示例：

```json
{
  "ok": true,
  "session_running": false,
  "ws_clients": 0,
  "has_primary": false,
  "nitrogen": null
}
```

会话运行中时，`nitrogen` 包含 `inference_count`、`timeout_count` 等调试字段。

### `POST /probe/tts-echo`

向当前会话的 TTS 队列注入测试短句「探针测试，链路正常。」

**前提：** 分析会话已在运行（探针第 3 步或手动 `POST /start`）。

```bash
curl -X POST http://localhost:8000/probe/tts-echo
```

成功响应：

```json
{ "status": "queued", "text": "探针测试，链路正常。" }
```

无会话时返回 `503`。

### 相关已有接口

| 接口 | 用途 |
|------|------|
| `GET /session/status` | 查询 `running` / `has_primary` |
| `POST /start` | 启动分析会话 |
| `POST /stop` | 停止分析会话 |

---

## 文件位置

| 文件 | 说明 |
|------|------|
| `frontend/probe.html` | 探针页面结构 |
| `frontend/probe.js` | 探针逻辑与步骤编排 |
| `frontend/probe.css` | 探针页样式 |
| `backend/main.py` | `/probe`、`/probe/health`、`/probe/tts-echo` 路由 |
| `tests/test_probe.py` | 探针 HTTP 端点单元测试 |

---

## 故障排查速查

1. **整页打不开** → 确认 `python run.py` 在跑，端口 8000 未被占用。
2. **`python run.py` 导入 whisper 报 TypeError (NoneType)** → 误装了 PyPI 包 `whisper`，应改为 `pip uninstall whisper -y` 后 `pip install openai-whisper`（见 `README.md`）。
3. **探针「WebSocket 注册为主连接」失败 (~300ms)** → 最常见：后端日志出现 `No supported WebSocket library` 与 `GET /ws ... 404`，执行 `pip install "uvicorn[standard]" websockets`（或 `pip install -r requirements.txt`）后 **Ctrl+C 重启** `python run.py`。探针第 1 步健康检查也会报 `websocket_ready=false`。其他原因：后端崩溃、地址栏须为 `http://localhost:8000/probe`（勿用 `file://`）、F12 → Network → WS 看连接是否被拒绝。
4. **TTS 步骤超时** → 检查 edge-tts 网络；查看后端日志是否有合成错误。
5. **perception 失败** → mock 模式（默认）下应能通过；live 模式需启动 ZMQ 并设 `NITROGEN_MOCK=0`
6. **Nginx 反代 ws-register 失败** → 需配置 WebSocket upgrade。
7. **与主应用冲突** → 先停主应用会话或等探针跑完再开主页面；重复点「启动」会 `POST /start` 409（会话已在运行，可忽略）。

---

## 相关文档

- 主应用使用：`README.md`
- WebSocket 协议与主连接模型：`DESIGN.md` §6.2
