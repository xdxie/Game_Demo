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
let isPrimaryClient = true;
let wsReconnectTimer = null;
let wsReconnectAttempts = 0;
const WS_RECONNECT_BASE_MS = 1000;
const WS_RECONNECT_MAX_MS = 15000;
const dismissedUtteranceIds = new Set();

// ── DOM 引用 ──────────────────────────────────────────────────────────
const $ = id => document.getElementById(id);
const videoPlayer   = $('video-player');
const fileInput     = $('file-input');
const uploadArea    = $('video-upload-area');
const playerArea    = $('video-player-area');
const btnStart      = $('btn-start-analysis');
const btnStop       = $('btn-stop-analysis');
const btnClearChat  = $('btn-clear-chat');
const chatMessages  = $('chat-messages');
const dotNitrogen   = $('dot-nitrogen');
const dotVLM        = $('dot-vlm');
const ttsStatus     = $('tts-status');
const micStatus     = $('mic-status');
const captureCanvas = $('capture-canvas');   // Fix 11
const captureCtx    = captureCanvas.getContext('2d');

// ── 文件选择 ──────────────────────────────────────────────────────────
fileInput.addEventListener('change', e => {
  const file = e.target.files[0];
  if (!file) return;

  uploadArea.style.display = 'none';
  playerArea.style.display = 'flex';

  videoPlayer.src = URL.createObjectURL(file);
  // Fix 11：视频加载后通过 WebSocket 发元数据，不再需要本地路径
});

// ── 开始/停止分析 ─────────────────────────────────────────────────────
btnStart.addEventListener('click', async () => {
  if (!videoPlayer.src) {
    alert('请先选择视频文件');
    return;
  }
  try {
    // Fix 11：不再传 video_path
    const resp = await fetch('/start', { method: 'POST' });
    const data = await resp.json();
    if (data.error) { alert('启动失败：' + data.error); return; }

    connectWebSocket();
    startMicrophone();
    isAnalysisRunning = true;
    btnStart.style.display = 'none';
    btnStop.style.display  = '';
    videoPlayer.play();
    addSystemMsg('分析已开始，持续收音中…');
  } catch (err) {
    alert('连接后端失败：' + err.message);
  }
});

btnStop.addEventListener('click', async () => {
  isAnalysisRunning = false;
  clearWsReconnectTimer();
  await fetch('/stop', { method: 'POST' });
  disconnectAll();
  btnStart.style.display = '';
  btnStop.style.display  = 'none';
  addSystemMsg('分析已停止');
});

btnClearChat.addEventListener('click', () => {
  chatMessages.innerHTML = '';
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'clear_conversation' }));
  }
});

// ── 视频元数据加载完成 → 通知后端 ─────────────────────────────────────
videoPlayer.addEventListener('loadedmetadata', () => {
  // 视频就绪，等用户点"开始分析"后通过 WS 发送时长
  // （此时 WS 可能还未建立，实际发送在 ws.onopen 后由帧捕获时序保证）
});

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

function applySessionRole(role) {
  isPrimaryClient = (role === 'primary');
  if (isPrimaryClient) {
    micStatus.textContent = '🎤 持续收音中';
    micStatus.className = '';
    sendVideoReadyIfNeeded();
    startFrameCapture();
  } else {
    micStatus.textContent = '👁 旁观（只读）';
    micStatus.className = '';
    stopFrameCapture();
    stopTTSAudio();
  }
}

function connectWebSocket() {
  clearWsReconnectTimer();
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(`${protocol}//${location.host}/ws`);
  ws.binaryType = 'arraybuffer';   // Fix 14：接收 ArrayBuffer 而非 Blob

  ws.onopen = () => {
    console.log('WebSocket connected');
    ws.send(JSON.stringify({ type: 'register', role: 'player' }));
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
      break;

    case 'tts':
      addChatMessage(msg.channel, msg.text, msg.video_time, msg.utterance_id);
      if (msg.playing && msg.utterance_id != null) {
        pendingUtteranceId = msg.utterance_id;
        ttsStatus.textContent = `▶ ${channelLabel(msg.channel)}: "${truncate(msg.text, 20)}"`;
      }
      break;

    case 'tts_interrupt':
      dismissUtterance(msg.utterance_id);
      if (msg.utterance_id == null
          || currentUtteranceId === msg.utterance_id
          || pendingUtteranceId === msg.utterance_id) {
        stopTTSAudio();
      }
      break;

    case 'tts_end':
      ttsStatus.textContent = '🔇 待机';
      clearPlayingHighlight();
      break;

    case 'asr_state':
      updateMicStatus(msg.state);
      break;

    case 'vlm_state':
      dotVLM.className = msg.busy ? 'dot active' : 'dot';
      break;

    case 'perception':
      $('dbg-intent').textContent  = msg.intent;
      $('dbg-conf').textContent    = (msg.confidence * 100).toFixed(0) + '%';
      $('dbg-dir').textContent     = msg.direction || '无';
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

function disconnectAll() {
  isAnalysisRunning = false;
  clearWsReconnectTimer();
  stopFrameCapture();
  stopTTSAudio();
  dismissedUtteranceIds.clear();
  if (ws)              { ws.close(); ws = null; }
  if (audioProcessor)  { audioProcessor.disconnect(); audioProcessor = null; }
  if (audioContext)    { audioContext.close(); audioContext = null; }
  if (mediaStream)     { mediaStream.getTracks().forEach(t => t.stop()); mediaStream = null; }
  dotNitrogen.className = 'dot';
  dotVLM.className      = 'dot';
  ttsStatus.textContent = '🔇 待机';
  micStatus.textContent = '🎤 未连接';
}
