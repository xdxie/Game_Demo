/**
 * 浏览器 E2E 链路探针
 *
 * 验证：HTTP 健康 → 会话启动 → WebSocket register → 合成帧推送 →
 *       TTS 合成+二进制回传+tts_done → 旁观连接
 *
 * 完成后写入 window.__PROBE_RESULT__，并派发 probe-complete 事件（供 Playwright 读取）。
 */
'use strict';

const STEPS = [
  { id: 'health',       name: '服务端健康检查',           critical: true },
  { id: 'status',       name: '会话状态 API',             critical: true },
  { id: 'start',        name: '启动分析会话',             critical: true },
  { id: 'ws-register',  name: 'WebSocket 注册为主连接',   critical: true },
  { id: 'video-ready',  name: 'video_ready 元数据',       critical: true },
  { id: 'push-frame',   name: '推送合成视频帧 (0x02)',    critical: true },
  { id: 'push-pcm',     name: '推送静音 PCM (0x01)',      critical: false },
  { id: 'tts-roundtrip', name: 'TTS 合成 → MP3 → tts_done', critical: true },
  { id: 'observer',     name: '旁观连接 register',        critical: false },
  { id: 'perception',   name: '感知回传 (perception)',    critical: true },
];

const PROBE_TTS_TEXT = '探针测试，链路正常。';
const TTS_TIMEOUT_MS = 45000;
const PERCEPTION_TIMEOUT_MS = 12000;

let probeWs = null;
let observerWs = null;
let probeStartedSession = false;
let running = false;

const $ = id => document.getElementById(id);
const logEl = $('log-output');
const stepsList = $('steps-list');
const summaryEl = $('summary');
const summaryText = $('summary-text');
const btnRun = $('btn-run');
const btnStopSession = $('btn-stop-session');

function log(msg, level = '') {
  const ts = new Date().toISOString().slice(11, 23);
  const line = document.createElement('div');
  line.innerHTML = `<span class="log-ts">${ts}</span> <span class="log-${level}">${escapeHtml(msg)}</span>`;
  logEl.appendChild(line);
  logEl.scrollTop = logEl.scrollHeight;
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

function initStepUI() {
  stepsList.innerHTML = '';
  for (const s of STEPS) {
    const li = document.createElement('li');
    li.className = 'step-item step-pending';
    li.id = `step-${s.id}`;
    li.innerHTML = `
      <span class="step-icon"></span>
      <span class="step-name">${s.name}</span>
      <span class="step-ms"></span>
      <div class="step-detail"></div>
    `;
    stepsList.appendChild(li);
  }
}

function setStepState(id, state, detail = '', ms = null) {
  const el = $(`step-${id}`);
  if (!el) return;
  el.className = `step-item step-${state}`;
  const detailEl = el.querySelector('.step-detail');
  if (detailEl) detailEl.textContent = detail;
  const msEl = el.querySelector('.step-ms');
  if (msEl && ms != null) msEl.textContent = `${ms}ms`;
}

function parseTTSBinaryFrame(arrayBuffer) {
  if (arrayBuffer.byteLength < 5) return null;
  const view = new DataView(arrayBuffer);
  if (view.getUint8(0) !== 0x03) return null;
  return {
    utteranceId: view.getUint32(1, true),
    mp3: arrayBuffer.slice(5),
  };
}

function wsUrl() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${proto}//${location.host}/ws`;
}

function openWebSocket() {
  const url = wsUrl();
  return new Promise((resolve, reject) => {
    let settled = false;
    const fail = (msg) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      reject(new Error(msg));
    };

    const ws = new WebSocket(url);
    ws.binaryType = 'arraybuffer';
    const timer = setTimeout(() => {
      ws.close();
      fail(`WebSocket 连接超时 (8s)：${url}`);
    }, 8000);

    ws.onopen = () => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(ws);
    };
    ws.onerror = () => {
      fail(
        `WebSocket 连接失败：${url}\n`
        + '常见原因：① python run.py 未运行或已崩溃 ② 地址/端口不对 ③ 应用页与后端不是同一 host'
      );
    };
    ws.onclose = (ev) => {
      if (settled) return;
      fail(
        `WebSocket 被关闭 (code=${ev.code})：${url}\n`
        + '请查看运行 python run.py 的终端是否有报错'
      );
    };
  });
}

function waitWsJson(ws, predicate, timeoutMs) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      ws.removeEventListener('message', onMsg);
      reject(new Error(`等待消息超时 (${timeoutMs}ms)`));
    }, timeoutMs);

    function onMsg(e) {
      if (typeof e.data !== 'string') return;
      try {
        const msg = JSON.parse(e.data);
        if (predicate(msg)) {
          clearTimeout(timer);
          ws.removeEventListener('message', onMsg);
          resolve(msg);
        }
      } catch { /* ignore */ }
    }
    ws.addEventListener('message', onMsg);
  });
}

/**
 * 在 POST /probe/tts-echo 之前挂上监听，避免服务端瞬时回推 tts/MP3 时丢消息。
 */
function waitTtsRoundtrip(ws, timeoutMs) {
  return new Promise((resolve, reject) => {
    let ttsJson = null;

    const timer = setTimeout(() => {
      ws.removeEventListener('message', onMsg);
      const hint = ttsJson
        ? `已收到 tts #${ttsJson.utterance_id}，未收到匹配 MP3（检查 edge-tts 网络）`
        : '未收到 tts JSON（队列忙、edge-tts 不可达或消息在监听前已发出）';
      reject(new Error(`TTS 往返超时 (${timeoutMs}ms)：${hint}`));
    }, timeoutMs);

    function cleanup() {
      clearTimeout(timer);
      ws.removeEventListener('message', onMsg);
    }

    function onMsg(e) {
      if (typeof e.data === 'string') {
        try {
          const msg = JSON.parse(e.data);
          if (
            msg.type === 'tts'
            && msg.utterance_id != null
            && msg.text === PROBE_TTS_TEXT
          ) {
            ttsJson = msg;
          }
        } catch { /* ignore */ }
        return;
      }
      if (!(e.data instanceof ArrayBuffer) || !ttsJson) return;
      const parsed = parseTTSBinaryFrame(e.data);
      if (parsed && parsed.utteranceId === ttsJson.utterance_id) {
        cleanup();
        resolve({ ttsJson, binary: parsed });
      }
    }

    ws.addEventListener('message', onMsg);
  });
}

function waitWsBinary(ws, predicate, timeoutMs) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      ws.removeEventListener('message', onMsg);
      reject(new Error(`等待二进制超时 (${timeoutMs}ms)`));
    }, timeoutMs);

    function onMsg(e) {
      if (!(e.data instanceof ArrayBuffer)) return;
      const parsed = predicate(e.data);
      if (parsed) {
        clearTimeout(timer);
        ws.removeEventListener('message', onMsg);
        resolve(parsed);
      }
    }
    ws.addEventListener('message', onMsg);
  });
}

async function makeSyntheticJpeg() {
  const canvas = $('probe-canvas');
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = '#1a3a5c';
  ctx.fillRect(0, 0, 256, 256);
  ctx.fillStyle = '#60a5fa';
  ctx.font = 'bold 20px sans-serif';
  ctx.fillText('PROBE', 88, 132);

  const blob = await new Promise((res, rej) => {
    canvas.toBlob(b => (b ? res(b) : rej(new Error('canvas toBlob failed'))), 'image/jpeg', 0.85);
  });
  return new Uint8Array(await blob.arrayBuffer());
}

function packVideoFrame(jpegBytes, videoTime = 1.0) {
  const buf = new ArrayBuffer(1 + 8 + jpegBytes.length);
  const view = new DataView(buf);
  view.setUint8(0, 0x02);
  view.setFloat64(1, videoTime, true);
  new Uint8Array(buf, 9).set(jpegBytes);
  return buf;
}

function packPcmSilence(sampleCount = 1600) {
  const buf = new ArrayBuffer(1 + sampleCount * 2);
  new DataView(buf).setUint8(0, 0x01);
  return buf;
}

async function runStep(stepDef, fn) {
  const t0 = performance.now();
  setStepState(stepDef.id, 'running');
  log(`▶ ${stepDef.name}`);
  try {
    const detail = await fn();
    const ms = Math.round(performance.now() - t0);
    setStepState(stepDef.id, 'pass', detail || 'OK', ms);
    log(`✓ ${stepDef.name} (${ms}ms)${detail ? ': ' + detail : ''}`, 'ok');
    return { id: stepDef.id, status: 'pass', detail, ms, critical: stepDef.critical };
  } catch (err) {
    const ms = Math.round(performance.now() - t0);
    const msg = err.message || String(err);
    if (stepDef.critical) {
      setStepState(stepDef.id, 'fail', msg, ms);
      log(`✗ ${stepDef.name}: ${msg}`, 'fail');
      return { id: stepDef.id, status: 'fail', detail: msg, ms, critical: true };
    }
    setStepState(stepDef.id, 'warn', msg, ms);
    log(`! ${stepDef.name} (非关键): ${msg}`, 'warn');
    return { id: stepDef.id, status: 'warn', detail: msg, ms, critical: false };
  }
}

async function closeProbeSockets() {
  for (const ws of [probeWs, observerWs]) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      try { ws.close(); } catch { /* ignore */ }
    }
  }
  probeWs = null;
  observerWs = null;
}

async function runAllProbes() {
  if (running) return;
  running = true;
  probeStartedSession = false;
  logEl.innerHTML = '';
  initStepUI();
  summaryEl.className = 'summary running';
  summaryText.textContent = '探针运行中…';
  btnRun.disabled = true;

  const results = [];

  const handlers = {
    health: async () => {
      const r = await fetch('/probe/health');
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const data = await r.json();
      if (!data.ok) throw new Error('health ok=false');
      if (data.websocket_ready === false) {
        throw new Error(
          '服务端未安装 websockets：请执行 pip install "uvicorn[standard]" websockets 后重启 python run.py'
        );
      }
      const backend = data.nitrogen_backend || data.nitrogen_mode || '?';
      const mode = backend === 'mock' ? 'mock(前端闭环)' : backend;
      return `session=${data.session_running}, ws=${data.ws_clients}, nitrogen=${mode}`;
    },
    status: async () => {
      const r = await fetch('/session/status');
      const data = await r.json();
      return `running=${data.running}, primary=${data.has_primary}`;
    },
    start: async () => {
      // 先停旧会话，确保用当前 NITROGEN_MOCK 配置新建 GameSession
      await fetch('/stop', { method: 'POST' });
      const r = await fetch('/start', { method: 'POST' });
      const data = await r.json();
      if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
      probeStartedSession = true;
      btnStopSession.disabled = false;
      const backend = data.nitrogen_backend || data.nitrogen_mode || '?';
      if (backend === 'fast_api') {
        return `新会话已启动 (nitrogen=fast_api，需 SSH 隧道 + 远端 /predict)`;
      }
      if (backend === 'zmq' || data.nitrogen_mode === 'live') {
        return `新会话已启动 (nitrogen=zmq，perception 需 ZMQ serve)`;
      }
      return `新会话已启动 (nitrogen=${backend})`;
    },
    'ws-register': async () => {
      await closeProbeSockets();
      probeWs = await openWebSocket();
      const rolePromise = waitWsJson(probeWs, m => m.type === 'session_role', 5000);
      probeWs.send(JSON.stringify({ type: 'register', role: 'player' }));
      const role = await rolePromise;
      if (role.role !== 'primary') throw new Error(`期望 primary，收到 ${role.role}`);
      return `role=${role.role}`;
    },
    'video-ready': async () => {
      probeWs.send(JSON.stringify({ type: 'video_ready', duration: 120.0 }));
      const status = await waitWsJson(
        probeWs, m => m.type === 'status' && m.state === 'video_ready', 5000,
      ).catch(() => null);
      if (status) return `duration=${status.duration}s`;
      return '已发送 (无 status 回显)';
    },
    'push-frame': async () => {
      const jpeg = await makeSyntheticJpeg();
      probeWs.send(packVideoFrame(jpeg, 2.5));
      await new Promise(r => setTimeout(r, 300));
      return `jpeg=${jpeg.length}B @ t=2.5s`;
    },
    'push-pcm': async () => {
      const pcm = packPcmSilence(1600);
      probeWs.send(pcm);
      return `pcm=${pcm.byteLength - 1}B`;
    },
    'tts-roundtrip': async () => {
      const roundtrip = waitTtsRoundtrip(probeWs, TTS_TIMEOUT_MS);
      const r = await fetch('/probe/tts-echo', { method: 'POST' });
      const data = await r.json();
      if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);

      const { ttsJson, binary } = await roundtrip;
      const uid = ttsJson.utterance_id;

      if (!binary.mp3 || binary.mp3.byteLength < 16) {
        throw new Error(`MP3 过短 (${binary.mp3?.byteLength || 0}B)`);
      }

      probeWs.send(JSON.stringify({ type: 'tts_done', utterance_id: uid }));
      await new Promise(res => setTimeout(res, 200));
      return `utterance_id=${uid}, mp3=${binary.mp3.byteLength}B`;
    },
    observer: async () => {
      observerWs = await openWebSocket();
      const rolePromise = waitWsJson(observerWs, m => m.type === 'session_role', 5000);
      observerWs.send(JSON.stringify({ type: 'register', role: 'observer' }));
      const role = await rolePromise;
      if (role.role !== 'observer') throw new Error(`期望 observer，收到 ${role.role}`);
      observerWs.close();
      observerWs = null;
      return `role=${role.role}`;
    },
    perception: async () => {
      const health = await fetch('/probe/health').then(x => x.json());
      const mockMode = health.nitrogen_mode === 'mock';
      const sessionMock = health.nitrogen?.mode === 'mock';

      const perceptionPromise = waitWsJson(
        probeWs, m => m.type === 'perception', PERCEPTION_TIMEOUT_MS,
      );

      const jpeg = await makeSyntheticJpeg();
      for (let i = 0; i < 8; i++) {
        probeWs.send(packVideoFrame(jpeg, 3.0 + i * 0.1));
        await new Promise(r => setTimeout(r, 80));
      }

      let p;
      try {
        p = await perceptionPromise;
      } catch (e) {
        if (!mockMode || sessionMock === false) {
          throw new Error(
            '未收到 perception：当前为 live 模式或未用 mock 会话。'
            + '请确认 .env 中 NITROGEN_MOCK=1，Ctrl+C 重启 python run.py 后重跑探针'
            + '（此步与 Anthropic/VLM 无关）'
          );
        }
        throw e;
      }

      const tag = (mockMode || sessionMock) ? 'mock' : 'live';
      return `intent=${p.intent}, conf=${(p.confidence * 100).toFixed(0)}% (${tag})`;
    },
  };

  try {
    for (const stepDef of STEPS) {
      const result = await runStep(stepDef, handlers[stepDef.id]);
      results.push(result);
      if (result.status === 'fail' && stepDef.critical) {
        log('关键步骤失败，中止后续探针', 'fail');
        break;
      }
    }
  } finally {
    await closeProbeSockets();
    running = false;
    btnRun.disabled = false;
  }

  publishResult(results);
}

function publishResult(results) {
  const failed = results.filter(r => r.status === 'fail');
  const warned = results.filter(r => r.status === 'warn');
  const passed = results.filter(r => r.status === 'pass');

  const payload = {
    ok: failed.length === 0,
    passed: passed.length,
    warned: warned.length,
    failed: failed.length,
    steps: results,
    ts: new Date().toISOString(),
  };
  window.__PROBE_RESULT__ = payload;
  window.dispatchEvent(new CustomEvent('probe-complete', { detail: payload }));

  if (failed.length > 0) {
    summaryEl.className = 'summary fail';
    summaryText.textContent = `失败 ${failed.length} 项关键探针 · 通过 ${passed.length}/${results.length}`;
  } else if (warned.length > 0) {
    summaryEl.className = 'summary partial';
    summaryText.textContent = `通过（${warned.length} 项警告）· ${passed.length}/${results.length}`;
  } else {
    summaryEl.className = 'summary pass';
    summaryText.textContent = `全部通过 · ${passed.length}/${results.length}`;
  }

  log(`── 探针结束: ${payload.ok ? 'PASS' : 'FAIL'} ──`, payload.ok ? 'ok' : 'fail');
}

async function stopProbeSession() {
  try {
    await fetch('/stop', { method: 'POST' });
    probeStartedSession = false;
    btnStopSession.disabled = true;
    log('已停止探针会话', 'ok');
  } catch (err) {
    log(`停止失败: ${err.message}`, 'fail');
  }
}

btnRun.addEventListener('click', () => runAllProbes());
btnStopSession.addEventListener('click', () => stopProbeSession());

initStepUI();

if (new URLSearchParams(location.search).get('autorun') === '1') {
  window.addEventListener('load', () => setTimeout(() => runAllProbes(), 300));
}
