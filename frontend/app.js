/**
 * NitroGen Game Coach 前端逻辑
 *
 * Fix 11：视频帧由前端 canvas 捕获后通过 WebSocket 推送给后端
 *   - setInterval 100ms（10fps），drawImage → toBlob(JPEG) → 二进制 WS 消息
 *   - 消息格式：[0x02][8字节 float64 LE 视频时间][JPEG bytes]
 *   - 连接后发送 register(player)，收到 session_role: primary 后推帧/收音
 *   - 断线后指数退避自动重连（分析进行中时）
 *
 * Fix 13（前端无感知，后端已处理）
 *
 * Fix 14：TTS 音频由后端发送 MP3 bytes，前端用 Audio API 播放
 *   - ws.binaryType = 'arraybuffer'
 *   - 服务端→客户端 binary = MP3 bytes（直接播放）
 *   - 播放结束后发送 {"type":"tts_done","utterance_id":N} 精确完成信号
 *   - 收到 tts_interrupt 时立即停止当前音频
 *   - 收到 asr_state 更新麦克风状态指示
 *
 * 二进制协议（客户端 → 服务端）：
 *   byte[0]=0x01  PCM 音频（麦克风）
 *   byte[0]=0x02  视频帧：byte[1..8]=float64 LE 时间，byte[9..]=JPEG
 *
 * 二进制协议（服务端 → 客户端）：
 *   byte[0]=0x03  TTS 音频：byte[1:5]=uint32 LE utterance_id，byte[5:]=MP3
 */

'use strict';

// ── 全局状态 ──────────────────────────────────────────────────────────
let ws            = null;
let audioContext  = null;
let mediaStream   = null;
let audioProcessor = null;
let captureInterval = null;     // Fix 11：帧捕获定时器
let currentTTSAudio = null;     // Fix 14：当前播放的 Audio 元素
let currentUtteranceId = null;  // 当前播报 utterance_id（与 tts_done 关联）
let pendingUtteranceId = null;  // 已收到 tts JSON、等待 MP3 的 id
let playingMsgEl      = null;   // 正在播报的气泡元素
let isSeeking     = false;
let seekDebounce  = null;
let isAnalysisRunning = false;
let isPrimaryClient = false;
let wsReconnectTimer = null;
let wsReconnectAttempts = 0;
const WS_RECONNECT_BASE_MS = 1000;
const WS_RECONNECT_MAX_MS = 15000;
const dismissedUtteranceIds = new Set();
let backendPreparePromise = null;
let timelineScanPromise = null;
let hiddenScanVideo = null;
let hiddenScanCanvas = null;

const clientMode = new URLSearchParams(location.search).get('mode') === 'observer'
  ? 'observer'
  : 'player';

// ── DOM 引用 ──────────────────────────────────────────────────────────
const $ = id => document.getElementById(id);
const videoPlayer   = $('video-player');
const fileInput     = $('file-input');
const uploadArea    = $('video-upload-area');
const playerArea    = $('video-player-area');
const btnStart      = $('btn-start-analysis');
const btnStop       = $('btn-stop-analysis');
const btnClearChat  = $('btn-clear-chat');
const prepareStatus = $('prepare-status');
const chatMessages  = $('chat-messages');
const dotNitrogen   = $('dot-nitrogen');
const dotVLM        = $('dot-vlm');
const ttsStatus     = $('tts-status');
const micStatus     = $('mic-status');
const captureCanvas = $('capture-canvas');   // Fix 11
const captureCtx    = captureCanvas.getContext('2d');
const chkVideoAudio = $('chk-video-audio');

// ── 文件选择 ──────────────────────────────────────────────────────────
fileInput.addEventListener('change', e => {
  const file = e.target.files[0];
  if (!file) return;

  uploadArea.style.display = 'none';
  playerArea.style.display = 'flex';

  videoPlayer.src = URL.createObjectURL(file);
  // 默认静音游戏原声，避免扬声器串音导致收音变差（可手动开启）
  videoPlayer.muted = true;
  if (chkVideoAudio) chkVideoAudio.checked = false;
  backendPreparePromise = null;
  timelineScanPromise = null;
  updateStartButtonState();
});

function canvasToDataUrl(canvas) {
  return new Promise((resolve, reject) => {
    canvas.toBlob(blob => {
      if (!blob) return reject(new Error('canvas toBlob failed'));
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.onerror = reject;
      reader.readAsDataURL(blob);
    }, 'image/jpeg', 0.85);
  });
}

function waitVideoSeeked(videoEl) {
  return new Promise(resolve => {
    videoEl.addEventListener('seeked', () => resolve(), { once: true });
  });
}

function getHiddenScanVideo() {
  if (!hiddenScanVideo) {
    hiddenScanVideo = document.createElement('video');
    hiddenScanVideo.muted = true;
    hiddenScanVideo.playsInline = true;
    hiddenScanVideo.preload = 'auto';
    hiddenScanVideo.style.cssText = 'position:fixed;left:-9999px;width:1px;height:1px;opacity:0';
    document.body.appendChild(hiddenScanVideo);
    hiddenScanCanvas = document.createElement('canvas');
    hiddenScanCanvas.width = 256;
    hiddenScanCanvas.height = 256;
  }
  return hiddenScanVideo;
}

/** 用隐藏 video 抽帧，不拖动主播放器进度（避免「快进扫一遍」） */
async function scanVideoForActionTimeline() {
  const duration = videoPlayer.duration;
  if (!duration || !isFinite(duration) || !videoPlayer.src) return;

  const interval = 2.0;
  const maxFrames = 90;
  const scanVideo = getHiddenScanVideo();
  const scanCtx = hiddenScanCanvas.getContext('2d');

  if (scanVideo.src !== videoPlayer.src) {
    scanVideo.src = videoPlayer.src;
    await new Promise((resolve, reject) => {
      scanVideo.onloadedmetadata = () => resolve();
      scanVideo.onerror = () => reject(new Error('hidden video load failed'));
    });
  }

  const frames = [];
  try {
    for (let t = 0; t < duration && frames.length < maxFrames; t += interval) {
      scanVideo.currentTime = Math.min(t, duration - 0.05);
      await waitVideoSeeked(scanVideo);
      scanCtx.drawImage(scanVideo, 0, 0, 256, 256);
      const jpeg_b64 = await canvasToDataUrl(hiddenScanCanvas);
      frames.push({ t_sec: Math.round(t * 10) / 10, jpeg_b64 });
    }

    const r = await fetch('/actions/ingest-batch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        duration_sec: duration,
        sample_interval_sec: interval,
        frames,
      }),
    });
    const data = await r.json();
    if (!r.ok && r.status !== 202) throw new Error(data.error || `HTTP ${r.status}`);

    if (data.status === 'accepted' || data.building) {
      pollActionTimelineReady().catch(() => {});
      return;
    }

    const prep = await fetch('/prepare/status').then(x => x.json()).catch(() => ({}));
    updatePrepareStatusLine(prep, data.key_actions);
    console.log('Action timeline', data.timeline);
  } catch (err) {
    console.warn('Action timeline scan failed', err);
    if (prepareStatus) {
      prepareStatus.textContent = `动作时间线失败: ${err.message}`;
      prepareStatus.className = 'prepare-status error';
    }
  }
}

async function pollActionTimelineReady(maxWaitMs = 120000) {
  const t0 = Date.now();
  while (Date.now() - t0 < maxWaitMs) {
    const r = await fetch('/actions/timeline');
    if (r.status === 202) {
      await new Promise(res => setTimeout(res, 500));
      continue;
    }
    if (r.ok) {
      const tl = await r.json();
      const prep = await fetch('/prepare/status').then(x => x.json()).catch(() => ({}));
      updatePrepareStatusLine(prep, (tl.key_actions || []).length);
      console.log('Action timeline ready', tl);
      return;
    }
    await new Promise(res => setTimeout(res, 500));
  }
}

function updatePrepareStatusLine(prep, keyActions) {
  if (!prepareStatus) return;
  const vlmMode = prep.vlm_mode || 'unknown';
  const vlmModel = prep.vlm_model || '';
  const keyHint = keyActions != null ? `动作时间线 ${keyActions} 条；` : '';
  let line = `${keyHint}Whisper/TTS 就绪；VLM=${vlmMode}`;
  if (vlmModel) line += ` (${vlmModel})`;
  if (vlmMode === 'mock') {
    line += ' — 请在项目根目录 .env 配置 VLM_API_KEY';
    prepareStatus.className = 'prepare-status error';
  } else {
    prepareStatus.className = 'prepare-status ready';
  }
  prepareStatus.textContent = line;
  prepareStatus.hidden = false;
}

function updateStartButtonState() {
  if (!btnStart || clientMode !== 'player') return;
  const hasVideo = Boolean(videoPlayer.src);
  btnStart.disabled = !hasVideo;
  if (!hasVideo) {
    btnStart.textContent = '▶ 开始分析';
    return;
  }
  fetch('/prepare/status')
    .then(r => r.json())
    .then(st => {
      if (st.status === 'ready') {
        btnStart.textContent = '▶ 开始分析';
        btnStart.disabled = false;
      } else if (st.status === 'loading') {
        btnStart.textContent = '预热中…';
        btnStart.disabled = true;
      } else {
        btnStart.textContent = '▶ 开始分析';
        btnStart.disabled = false;
      }
    })
    .catch(() => {
      btnStart.textContent = '▶ 开始分析';
      btnStart.disabled = false;
    });
}

/** 选视频后即预热 Whisper/TTS；返回在就绪时 resolve 的 Promise */
function startBackendPrepare() {
  if (clientMode !== 'player') return Promise.resolve();
  if (backendPreparePromise) return backendPreparePromise;

  backendPreparePromise = (async () => {
    if (prepareStatus) {
      prepareStatus.hidden = false;
      prepareStatus.textContent = '正在加载 Whisper 与 TTS…';
      prepareStatus.className = 'prepare-status loading';
    }
    updateStartButtonState();

    try {
      const r = await fetch('/prepare?wait=true', { method: 'POST' });
      const st = await r.json();
      if (st.status === 'error') {
        throw new Error(st.error || '预热失败');
      }
      updatePrepareStatusLine(st, null);
      updateStartButtonState();
      return st;
    } catch (err) {
      if (prepareStatus) {
        prepareStatus.textContent = `预热失败: ${err.message}`;
        prepareStatus.className = 'prepare-status error';
      }
      updateStartButtonState();
      throw err;
    }
  })();

  return backendPreparePromise;
}

// ── 开始/停止分析（player 模式）────────────────────────────────────────
if (clientMode === 'player') {
  btnStart.addEventListener('click', async () => {
    if (!videoPlayer.src) {
      alert('请先选择视频文件');
      return;
    }
    const prevLabel = btnStart.textContent;
    btnStart.disabled = true;
    btnStart.textContent = '启动中…';
    try {
      const prepSt = await fetch('/prepare/status').then(r => r.json()).catch(() => ({}));
      if (prepSt.status !== 'ready') {
        btnStart.textContent = '预热中…';
        await startBackendPrepare();
      }

      const resp = await fetch('/start', { method: 'POST' });
      const raw = await resp.text();
      let data;
      try {
        data = JSON.parse(raw);
      } catch {
        throw new Error(raw.slice(0, 120) || `HTTP ${resp.status}`);
      }
      if (resp.status === 409) {
        alert(data.error || '分析已在运行');
        return;
      }
      if (data.error) { alert('启动失败：' + data.error); return; }

      if (data.vlm_mode === 'mock') {
        addSystemMsg(
          'VLM 当前为 mock 模式（会复述你的话）。请在 run.py 同目录 .env 配置 VLM_API_KEY 后重启服务。'
        );
      }

      isAnalysisRunning = true;
      try {
        await videoPlayer.play();
      } catch (err) {
        console.warn('video play:', err);
      }
      connectWebSocket();
      btnStart.style.display = 'none';
      btnStop.style.display  = '';
      addSystemMsg('分析已开始，等待主连接确认…');
    } catch (err) {
      alert('连接后端失败：' + err.message);
    } finally {
      btnStart.disabled = false;
      btnStart.textContent = prevLabel;
      updateStartButtonState();
    }
  });

  btnStop.addEventListener('click', async () => {
    isAnalysisRunning = false;
    clearWsReconnectTimer();
    disconnectAll();
    btnStart.style.display = '';
    btnStop.style.display  = 'none';
    addSystemMsg('分析已停止');
    fetch('/stop', { method: 'POST' }).catch(() => {});
  });
} else {
  initObserverMode();
}

btnClearChat.addEventListener('click', () => {
  chatMessages.innerHTML = '';
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'clear_conversation' }));
  }
});

// ── 视频元数据加载完成 → 预热 + 抽帧生成动作时间线 ───────────────────
videoPlayer.addEventListener('loadedmetadata', () => {
  if (clientMode !== 'player') return;
  if (prepareStatus) {
    prepareStatus.hidden = false;
    prepareStatus.textContent = '正在后台准备（Whisper/TTS + 动作时间线）…';
    prepareStatus.className = 'prepare-status loading';
  }
  startBackendPrepare().catch(() => {});
  timelineScanPromise = scanVideoForActionTimeline();
  updateStartButtonState();
});

if (chkVideoAudio) {
  chkVideoAudio.addEventListener('change', () => {
    videoPlayer.muted = !chkVideoAudio.checked;
  });
}

// ── WebSocket ─────────────────────────────────────────────────────────
function clearWsReconnectTimer() {
  if (wsReconnectTimer) {
    clearTimeout(wsReconnectTimer);
    wsReconnectTimer = null;
  }
}

function scheduleWsReconnect() {
  if (!isAnalysisRunning) return;
  clearWsReconnectTimer();
  const delay = Math.min(
    WS_RECONNECT_BASE_MS * Math.pow(2, wsReconnectAttempts),
    WS_RECONNECT_MAX_MS,
  );
  wsReconnectAttempts += 1;
  addSystemMsg(`连接断开，${(delay / 1000).toFixed(0)}s 后重连…`);
  wsReconnectTimer = setTimeout(() => {
    if (isAnalysisRunning) connectWebSocket();
  }, delay);
}

function sendVideoReadyIfNeeded() {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  if (videoPlayer.duration && isFinite(videoPlayer.duration)) {
    ws.send(JSON.stringify({
      type: 'video_ready',
      duration: videoPlayer.duration,
    }));
  }
}

/** 主连接接管或重连后，将当前视频进度同步给后端 */
function syncPrimaryStateToServer() {
  if (!isPrimaryClient || !ws || ws.readyState !== WebSocket.OPEN) return;
  sendVideoReadyIfNeeded();
  isSeeking = true;
  ws.send(JSON.stringify({ type: 'seek', time: videoPlayer.currentTime }));
  const shouldResume = isAnalysisRunning && !videoPlayer.paused && !videoPlayer.ended;
  ws.send(JSON.stringify({
    type: 'playback',
    action: shouldResume ? 'resume' : 'pause',
  }));
}

function applySessionRole(role) {
  const wasPrimary = isPrimaryClient;
  isPrimaryClient = (role === 'primary');
  if (isPrimaryClient) {
    ensureMicrophoneReady();
    syncPrimaryStateToServer();
    startFrameCapture();
    if (!wasPrimary) {
      addSystemMsg('已接管主连接');
    }
  } else {
    stopMicrophone();
    micStatus.textContent = '👁 旁观（只读）';
    micStatus.className = '';
    stopFrameCapture();
    stopTTSAudio();
  }
}

async function ensureMicrophoneReady() {
  if (!isPrimaryClient) return;
  try {
    if (!mediaStream || !audioContext || audioContext.state === 'closed') {
      await startMicrophone();
    } else if (audioContext.state === 'suspended') {
      await audioContext.resume();
    }
    updateMicStatus('listening');
  } catch (err) {
    console.error('ensureMicrophoneReady:', err);
  }
}

function connectWebSocket() {
  clearWsReconnectTimer();
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(`${protocol}//${location.host}/ws`);
  ws.binaryType = 'arraybuffer';   // Fix 14：接收 ArrayBuffer 而非 Blob

  ws.onopen = () => {
    console.log('WebSocket connected');
    ws.send(JSON.stringify({ type: 'register', role: clientMode }));
  };

  ws.onclose = () => {
    console.log('WebSocket closed');
    stopFrameCapture();
    if (!isAnalysisRunning) {
      micStatus.textContent = '🎤 未连接';
      return;
    }
    if (!isPrimaryClient) {
      micStatus.textContent = '👁 旁观（只读）';
    } else {
      micStatus.textContent = '🎤 未连接';
    }
    scheduleWsReconnect();
  };

  ws.onerror = () => {
    console.warn('WebSocket error');
  };

  ws.onmessage = e => {
    if (e.data instanceof ArrayBuffer) {
      if (!isPrimaryClient) return;
      const parsed = parseTTSBinaryFrame(e.data);
      if (parsed) {
        playTTSAudio(parsed.mp3, parsed.utteranceId);
      }
      return;
    }
    try {
      handleServerMessage(JSON.parse(e.data));
    } catch { /* ignore */ }
  };
}

function handleServerMessage(msg) {
  switch (msg.type) {

    case 'session_role':
      wsReconnectAttempts = 0;
      applySessionRole(msg.role);
      break;

    case 'primary_changed':
      addSystemMsg('主连接已切换');
      if (isPrimaryClient) {
        syncPrimaryStateToServer();
      }
      break;

    case 'tts':
      if (msg.text) {
        addChatMessage(msg.channel, msg.text, msg.video_time, msg.utterance_id);
      }
      if (msg.playing && msg.utterance_id != null) {
        pendingUtteranceId = msg.utterance_id;
        ttsStatus.textContent = `▶ ${channelLabel(msg.channel)}: "${truncate(msg.text || '', 20)}"`;
      } else if (msg.synthesizing) {
        ttsStatus.textContent = '🔊 正在合成语音…';
      }
      break;

    case 'tts_interrupt':
      dismissUtterance(msg.utterance_id);
      if (msg.utterance_id == null
          || currentUtteranceId === msg.utterance_id
          || pendingUtteranceId === msg.utterance_id) {
        stopTTSAudio();
        ttsStatus.textContent = '🔇 待机';
      }
      break;

    case 'tts_end':
      ttsStatus.textContent = '🔇 待机';
      clearPlayingHighlight();
      break;

    case 'asr_state':
      if (isPrimaryClient) updateMicStatus(msg.state);
      break;

    case 'vlm_state':
      dotVLM.className = msg.busy ? 'dot active' : 'dot';
      break;

    case 'perception':
      $('dbg-intent').textContent  = msg.intent;
      $('dbg-conf').textContent    = (msg.confidence * 100).toFixed(0) + '%';
      $('dbg-dir').textContent     = msg.direction || '无';
      $('dbg-steer').textContent   = msg.steer != null ? msg.steer.toFixed(2) : '—';
      $('dbg-throttle').textContent = msg.throttle != null ? String(msg.throttle) : '—';
      $('dbg-brake').textContent   = msg.brake != null ? String(msg.brake) : '—';
      $('dbg-hint').textContent    = msg.hint || (msg.is_change ? '动作变化' : '—');
      $('dbg-horizon').textContent = (msg.horizon || []).join(' → ');
      $('dbg-time').textContent    = (msg.video_time ?? 0).toFixed(2) + 's';
      dotNitrogen.className = 'dot active';
      break;

    case 'seek_done':
      isSeeking = false;
      stopTTSAudio();
      break;

    case 'status':
      if (msg.state === 'started') dotNitrogen.className = 'dot loading';
      if (msg.state === 'user_question_no_frame') {
        addSystemMsg('画面未就绪，暂时无法回答，请稍后再问');
      }
      break;

    case 'video_ended':
      addSystemMsg('视频播放结束');
      ttsStatus.textContent = '🔇 待机';
      break;

    case 'conversation_cleared':
      chatMessages.innerHTML = '';
      break;
  }
}

// ── Fix 11：视频帧捕获 ────────────────────────────────────────────────

function startFrameCapture() {
  if (captureInterval) return;
  // 100ms = 10fps，与 NitroGen 推理频率对齐
  captureInterval = setInterval(captureAndSendFrame, 100);
}

function stopFrameCapture() {
  if (captureInterval) {
    clearInterval(captureInterval);
    captureInterval = null;
  }
}

function captureAndSendFrame() {
  if (!isPrimaryClient || !ws || ws.readyState !== WebSocket.OPEN) return;
  if (videoPlayer.paused || videoPlayer.ended || videoPlayer.readyState < 2) return;

  // 将当前视频帧绘制到 256×256 canvas
  captureCtx.drawImage(videoPlayer, 0, 0, 256, 256);

  captureCanvas.toBlob(blob => {
    if (!blob || !ws || ws.readyState !== WebSocket.OPEN) return;

    const videoTime = videoPlayer.currentTime;

    blob.arrayBuffer().then(jpegBuf => {
      // 构造消息：[0x02][8字节 float64 LE 时间][JPEG bytes]
      const header = new ArrayBuffer(9);
      const view   = new DataView(header);
      view.setUint8(0, 0x02);
      view.setFloat64(1, videoTime, true);  // little-endian

      const msg = new Uint8Array(9 + jpegBuf.byteLength);
      msg.set(new Uint8Array(header), 0);
      msg.set(new Uint8Array(jpegBuf), 9);
      ws.send(msg.buffer);
    });
  }, 'image/jpeg', 0.85);
}

// ── 视频进度条同步 ────────────────────────────────────────────────────
videoPlayer.addEventListener('seeking', () => {
  if (seekDebounce) clearTimeout(seekDebounce);
  seekDebounce = setTimeout(() => {
    dismissUtterance(pendingUtteranceId);
    dismissUtterance(currentUtteranceId);
    stopTTSAudio();
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'seek', time: videoPlayer.currentTime }));
      isSeeking = true;
    }
  }, 200);
});

videoPlayer.addEventListener('pause', () => {
  stopTTSAudio();
  if (ws && ws.readyState === WebSocket.OPEN)
    ws.send(JSON.stringify({ type: 'playback', action: 'pause' }));
});

videoPlayer.addEventListener('play', () => {
  if (ws && ws.readyState === WebSocket.OPEN)
    ws.send(JSON.stringify({ type: 'playback', action: 'resume' }));
});

videoPlayer.addEventListener('ended', () => {
  if (ws && ws.readyState === WebSocket.OPEN)
    ws.send(JSON.stringify({ type: 'video_ended' }));
});

// ── Fix 14：TTS 音频播放 ──────────────────────────────────────────────

/** 解析服务端 TTS 二进制帧：0x03 + uint32 LE utterance_id + MP3 */
function parseTTSBinaryFrame(arrayBuffer) {
  if (arrayBuffer.byteLength < 5) return null;
  const view = new DataView(arrayBuffer);
  if (view.getUint8(0) !== 0x03) return null;
  return {
    utteranceId: view.getUint32(1, true),
    mp3: arrayBuffer.slice(5),
  };
}

function dismissUtterance(utteranceId) {
  if (utteranceId != null) dismissedUtteranceIds.add(utteranceId);
}

function sendTtsDone(utteranceId) {
  if (!isPrimaryClient) return;
  if (ws && ws.readyState === WebSocket.OPEN && utteranceId != null) {
    ws.send(JSON.stringify({ type: 'tts_done', utterance_id: utteranceId }));
  }
}

function stopCurrentTTSAudio() {
  if (currentTTSAudio) {
    currentTTSAudio.pause();
    currentTTSAudio.src = '';
    currentTTSAudio = null;
  }
  currentUtteranceId = null;
  clearPlayingHighlight();
}

function stopTTSAudio() {
  stopCurrentTTSAudio();
  pendingUtteranceId = null;
  ttsStatus.textContent = '🔇 待机';
}

function playTTSAudio(arrayBuffer, utteranceIdFromFrame) {
  const utteranceId = utteranceIdFromFrame ?? pendingUtteranceId;
  pendingUtteranceId = null;

  if (isSeeking) {
    sendTtsDone(utteranceId);
    return;
  }
  if (utteranceId != null && dismissedUtteranceIds.has(utteranceId)) {
    sendTtsDone(utteranceId);
    return;
  }

  stopCurrentTTSAudio();
  currentUtteranceId = utteranceId;

  const blob = new Blob([arrayBuffer], { type: 'audio/mpeg' });
  const url  = URL.createObjectURL(blob);
  const audio = new Audio(url);

  const onPlaybackEnd = () => {
    URL.revokeObjectURL(url);
    currentTTSAudio = null;
    sendTtsDone(utteranceId);
    currentUtteranceId = null;
    clearPlayingHighlight();
    ttsStatus.textContent = '🔇 待机';
  };

  audio.onended = onPlaybackEnd;

  audio.onerror = onPlaybackEnd;

  audio.play().catch(err => {
    console.warn('TTS audio play error (may need user gesture):', err);
    onPlaybackEnd();
  });

  currentTTSAudio = audio;
  highlightPlayingMessage(utteranceId);
}

// ── 麦克风采集（Web Audio API + AudioWorklet）────────────────────────
async function startMicrophone() {
  try {
    mediaStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      }
    });

    audioContext = new AudioContext();
    await audioContext.resume();

    const source = audioContext.createMediaStreamSource(mediaStream);
    const frameSize = Math.round(audioContext.sampleRate * 0.1);

    if (audioContext.audioWorklet) {
      await audioContext.audioWorklet.addModule('/static/pcm-processor.worklet.js');
      audioProcessor = new AudioWorkletNode(audioContext, 'pcm-processor', {
        processorOptions: {
          frameSize,
          targetSampleRate: 16000,
        },
      });
      audioProcessor.port.onmessage = e => {
        if (!isPrimaryClient || !ws || ws.readyState !== WebSocket.OPEN) return;
        const pcm16 = float32ToPCM16(e.data);
        const msg = new Uint8Array(1 + pcm16.byteLength);
        msg[0] = 0x01;
        msg.set(new Uint8Array(pcm16), 1);
        ws.send(msg.buffer);
      };
      const silentGain = audioContext.createGain();
      silentGain.gain.value = 0;
      source.connect(audioProcessor);
      audioProcessor.connect(silentGain);
      silentGain.connect(audioContext.destination);
    } else {
      startMicrophoneScriptProcessor(source, frameSize);
    }

    console.log('Microphone started @', audioContext.sampleRate, 'Hz');
    updateMicStatus('listening');
  } catch (err) {
    console.error('Mic error:', err);
    micStatus.textContent = '🎤 无权限';
  }
}

/** ScriptProcessor 回退（旧浏览器）；不将麦克风路由到扬声器。 */
function startMicrophoneScriptProcessor(source, frameSize) {
  const bufferSize = Math.max(256, Math.min(16384, frameSize));
  audioProcessor = audioContext.createScriptProcessor(bufferSize, 1, 1);

  audioProcessor.onaudioprocess = e => {
    if (!isPrimaryClient || !ws || ws.readyState !== WebSocket.OPEN) return;
    const float32 = e.inputBuffer.getChannelData(0);
    const pcm16 = float32ToPCM16(
      audioContext.sampleRate === 16000
        ? float32
        : resampleFloat32(float32, audioContext.sampleRate, 16000),
    );
    const msg = new Uint8Array(1 + pcm16.byteLength);
    msg[0] = 0x01;
    msg.set(new Uint8Array(pcm16), 1);
    ws.send(msg.buffer);
  };

  const silentGain = audioContext.createGain();
  silentGain.gain.value = 0;
  source.connect(audioProcessor);
  audioProcessor.connect(silentGain);
  silentGain.connect(audioContext.destination);
}

function resampleFloat32(float32, inputRate, targetRate) {
  if (inputRate === targetRate) return float32;
  const ratio = inputRate / targetRate;
  const outLen = Math.max(1, Math.round(float32.length / ratio));
  const out = new Float32Array(outLen);
  for (let i = 0; i < outLen; i++) {
    const src = i * ratio;
    const idx = Math.floor(src);
    const frac = src - idx;
    const s0 = float32[idx] ?? 0;
    const s1 = float32[idx + 1] ?? s0;
    out[i] = s0 + frac * (s1 - s0);
  }
  return out;
}

function float32ToPCM16(float32) {
  const buf  = new ArrayBuffer(float32.length * 2);
  const view = new DataView(buf);
  for (let i = 0; i < float32.length; i++) {
    const s = Math.max(-1, Math.min(1, float32[i]));
    view.setInt16(i * 2, s < 0 ? s * 32768 : s * 32767, true);
  }
  return buf;
}

// ── 对话面板 ──────────────────────────────────────────────────────────
function addChatMessage(channel, text, videoTime, utteranceId) {
  const placeholder = chatMessages.querySelector('.chat-placeholder');
  if (placeholder) placeholder.remove();

  const timeStr = videoTime != null ? formatTime(videoTime) : '';
  const el = document.createElement('div');
  el.className = `msg ${channel === 'user_answer' ? 'answer' : channel}`;
  if (utteranceId != null) {
    el.dataset.utteranceId = String(utteranceId);
  }
  el.innerHTML = `
    <div class="msg-header">
      <span class="msg-tag ${channel === 'user_answer' ? 'answer' : channel}">${channelLabel(channel)}</span>
      <span>${timeStr}</span>
    </div>
    <div class="msg-body">${escapeHtml(text)}</div>
  `;
  chatMessages.appendChild(el);
  chatMessages.scrollTop = chatMessages.scrollHeight;
  return el;
}

function highlightPlayingMessage(utteranceId) {
  clearPlayingHighlight();
  if (utteranceId == null) return;
  const el = chatMessages.querySelector(`[data-utterance-id="${utteranceId}"]`);
  if (el) {
    el.classList.add('playing');
    playingMsgEl = el;
  }
}

function clearPlayingHighlight() {
  if (playingMsgEl) {
    playingMsgEl.classList.remove('playing');
    playingMsgEl = null;
  }
}

function updateMicStatus(state) {
  if (!isPrimaryClient) return;
  const labels = {
    listening:  ['🎤 持续收音中', ''],
    recording:  ['🎤● 正在说话', 'recording'],
    processing: ['🎤 识别中…', 'recording'],
    muted:      ['🎤⊘ TTS 播报中', 'muted'],
  };
  const [text, cls] = labels[state] || ['🎤 持续收音中', ''];
  micStatus.textContent = text;
  micStatus.className   = cls;
}

function addSystemMsg(text) {
  const el = document.createElement('div');
  el.style.cssText = 'color:#64748b;font-size:12px;text-align:center;padding:4px 0';
  el.textContent = text;
  chatMessages.appendChild(el);
  chatMessages.scrollTop = chatMessages.scrollHeight;
}

// ── 工具函数 ──────────────────────────────────────────────────────────
function channelLabel(ch) {
  return { fast: 'AI-快', slow: 'AI-慢', user_answer: 'AI', user: '你' }[ch] || ch;
}
function formatTime(sec) {
  const m = Math.floor(sec / 60);
  const s = (sec % 60).toFixed(1).padStart(4, '0');
  return `${m}:${s}`;
}
function truncate(str, n) {
  return str.length > n ? str.slice(0, n) + '…' : str;
}
function escapeHtml(str) {
  return str
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function stopMicrophone() {
  if (audioProcessor) {
    audioProcessor.disconnect();
    audioProcessor = null;
  }
  if (audioContext) {
    audioContext.close();
    audioContext = null;
  }
  if (mediaStream) {
    mediaStream.getTracks().forEach(t => t.stop());
    mediaStream = null;
  }
}

function initObserverMode() {
  document.querySelector('.title').textContent = '陪玩（旁观）';
  const linkObs = $('link-observer');
  if (linkObs) linkObs.style.display = 'none';
  uploadArea.innerHTML = `
    <p>旁观模式：实时查看 AI 对话与 NitroGen 感知信号</p>
    <p class="hint">请先在主页面开始分析，再点击连接</p>
    <button type="button" id="btn-attach-observer" class="btn btn-primary">连接会话</button>
    <button type="button" id="btn-detach-observer" class="btn" style="display:none;margin-left:8px">断开</button>
  `;
  playerArea.style.display = 'none';
  btnClearChat.style.display = 'none';
  micStatus.textContent = '👁 旁观';

  $('btn-attach-observer').addEventListener('click', attachAsObserver);
  $('btn-detach-observer').addEventListener('click', detachObserver);
}

async function attachAsObserver() {
  try {
    const resp = await fetch('/session/status');
    const data = await resp.json();
    if (!data.running) {
      alert('当前没有运行中的分析会话，请先在主页面点击「开始分析」');
      return;
    }
    isAnalysisRunning = true;
    connectWebSocket();
    $('btn-attach-observer').style.display = 'none';
    $('btn-detach-observer').style.display = '';
    addSystemMsg('正在连接旁观会话…');
  } catch (err) {
    alert('连接失败：' + err.message);
  }
}

function detachObserver() {
  isAnalysisRunning = false;
  clearWsReconnectTimer();
  if (ws) { ws.close(); ws = null; }
  $('btn-attach-observer').style.display = '';
  $('btn-detach-observer').style.display = 'none';
  micStatus.textContent = '👁 旁观';
  addSystemMsg('已断开旁观连接');
}

function disconnectAll() {
  isAnalysisRunning = false;
  clearWsReconnectTimer();
  stopFrameCapture();
  stopTTSAudio();
  dismissedUtteranceIds.clear();
  if (ws)              { ws.close(); ws = null; }
  stopMicrophone();
  dotNitrogen.className = 'dot';
  dotVLM.className      = 'dot';
  ttsStatus.textContent = '🔇 待机';
  micStatus.textContent = '🎤 未连接';
}
