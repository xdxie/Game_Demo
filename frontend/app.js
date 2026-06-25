/**
 * NitroGen Game Coach 前端逻辑
 *
 * 职责：
 * 1. 视频文件选择与 HTML5 播放
 * 2. WebSocket 连接与消息处理
 * 3. Web Audio API 麦克风采集 → 发送 PCM 至后端
 * 4. 对话面板渲染（快/慢/用户/AI回答）
 * 5. 进度条拖动 → 发送 seek 事件
 * 6. 调试面板更新
 *
 * 注意（6号调优）：
 * - 麦克风采集参数：sampleRate=16000, channelCount=1（与 Whisper 一致）
 * - 音频块大小：1600 samples ≈ 100ms，与 ASRHandler 的 VAD chunk 假设一致
 * - 进度条拖动防抖：200ms，避免频繁 seek
 */

'use strict';

// ── 全局状态 ──────────────────────────────────────────────────────────
let ws = null;
let audioContext = null;
let mediaStream = null;
let audioProcessor = null;
let videoFilePath = null;  // 后端文件路径（demo 中直接传本地路径）
let isSeeking = false;
let seekDebounce = null;

// ── DOM 引用 ──────────────────────────────────────────────────────────
const $ = id => document.getElementById(id);
const videoPlayer    = $('video-player');
const fileInput      = $('file-input');
const uploadArea     = $('video-upload-area');
const playerArea     = $('video-player-area');
const btnStart       = $('btn-start-analysis');
const btnStop        = $('btn-stop-analysis');
const btnClearChat   = $('btn-clear-chat');
const chatMessages   = $('chat-messages');
const dotNitrogen    = $('dot-nitrogen');
const dotVLM         = $('dot-vlm');
const ttsStatus      = $('tts-status');
const micStatus      = $('mic-status');

// ── 文件选择 ──────────────────────────────────────────────────────────
fileInput.addEventListener('change', e => {
  const file = e.target.files[0];
  if (!file) return;

  // 展示视频播放器
  uploadArea.style.display  = 'none';
  playerArea.style.display  = 'flex';

  // 使用 object URL 在浏览器内播放（预览用）
  videoPlayer.src = URL.createObjectURL(file);

  // 后端需要本地文件路径（demo 模式：假设路径即文件名，实际部署可改为上传）
  // 这里通过 file.name 传递，demo 时在同一机器上 file.path 才有效
  // Electron 或直接访问本地文件时可用 file.path
  videoFilePath = file.path || file.name;
});

// ── 开始/停止分析 ─────────────────────────────────────────────────────
btnStart.addEventListener('click', async () => {
  if (!videoFilePath) {
    alert('请先选择视频文件');
    return;
  }
  try {
    const resp = await fetch('/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ video_path: videoFilePath }),
    });
    const data = await resp.json();
    if (data.error) {
      alert('启动失败：' + data.error);
      return;
    }
    connectWebSocket();
    startMicrophone();
    btnStart.style.display = 'none';
    btnStop.style.display  = '';
    videoPlayer.play();
    addSystemMsg('分析已开始，持续收音中…');
  } catch (err) {
    alert('连接后端失败：' + err.message);
  }
});

btnStop.addEventListener('click', async () => {
  await fetch('/stop', { method: 'POST' });
  disconnectAll();
  btnStart.style.display = '';
  btnStop.style.display  = 'none';
  addSystemMsg('分析已停止');
});

btnClearChat.addEventListener('click', () => {
  chatMessages.innerHTML = '';
});

// ── WebSocket ─────────────────────────────────────────────────────────
function connectWebSocket() {
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(`${protocol}//${location.host}/ws`);

  ws.onopen = () => {
    console.log('WebSocket connected');
    micStatus.textContent = '🎤 持续收音中';
    micStatus.className   = '';
  };

  ws.onclose = () => {
    console.log('WebSocket closed');
    micStatus.textContent = '🎤 未连接';
  };

  ws.onmessage = e => {
    try {
      const msg = JSON.parse(e.data);
      handleServerMessage(msg);
    } catch { /* ignore non-JSON */ }
  };
}

function handleServerMessage(msg) {
  switch (msg.type) {

    case 'tts':
      // 语音播报事件 → 添加到对话面板
      addChatMessage(msg.channel, msg.text, msg.video_time);
      if (msg.playing) {
        ttsStatus.textContent = `▶ ${channelLabel(msg.channel)}: "${truncate(msg.text, 20)}"`;
        dotNitrogen.className = 'dot active';
      }
      if (msg.channel === 'user') {
        micStatus.textContent = '🎤 识别中…';
      }
      break;

    case 'tts_end':
      ttsStatus.textContent = '🔇 待机';
      micStatus.textContent = '🎤 持续收音中';
      micStatus.className   = '';
      break;

    case 'perception':
      // 更新调试面板
      $('dbg-intent').textContent  = msg.intent;
      $('dbg-conf').textContent    = (msg.confidence * 100).toFixed(0) + '%';
      $('dbg-dir').textContent     = msg.direction || '无';
      $('dbg-horizon').textContent = (msg.horizon || []).join(' → ');
      $('dbg-time').textContent    = msg.video_time?.toFixed(2) + 's';
      dotNitrogen.className = 'dot active';
      break;

    case 'status':
      if (msg.state === 'started') {
        dotNitrogen.className = 'dot loading';
      }
      break;

    case 'seek_done':
      isSeeking = false;
      break;

    case 'video_ended':
      addSystemMsg('视频播放结束');
      ttsStatus.textContent = '🔇 待机';
      break;
  }
}

// ── 视频进度条同步 → seek ─────────────────────────────────────────────
videoPlayer.addEventListener('seeking', () => {
  if (seekDebounce) clearTimeout(seekDebounce);
  seekDebounce = setTimeout(() => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'seek', time: videoPlayer.currentTime }));
      isSeeking = true;
    }
  }, 200);
});

videoPlayer.addEventListener('pause', () => {
  if (ws && ws.readyState === WebSocket.OPEN)
    ws.send(JSON.stringify({ type: 'playback', action: 'pause' }));
  micStatus.textContent = '🎤 已暂停';
});

videoPlayer.addEventListener('play', () => {
  if (ws && ws.readyState === WebSocket.OPEN)
    ws.send(JSON.stringify({ type: 'playback', action: 'resume' }));
  micStatus.textContent = '🎤 持续收音中';
});

// ── 麦克风采集（Web Audio API）────────────────────────────────────────
async function startMicrophone() {
  try {
    mediaStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        sampleRate: 16000,
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
      }
    });

    audioContext = new AudioContext({ sampleRate: 16000 });
    const source = audioContext.createMediaStreamSource(mediaStream);

    // ScriptProcessor（简单兼容方案，后续可升级为 AudioWorklet）
    // bufferSize=1600 → 100ms @ 16kHz
    audioProcessor = audioContext.createScriptProcessor(1600, 1, 1);
    audioProcessor.onaudioprocess = e => {
      if (!ws || ws.readyState !== WebSocket.OPEN) return;
      const float32 = e.inputBuffer.getChannelData(0);
      const pcm16 = float32ToPCM16(float32);
      ws.send(pcm16);
    };

    source.connect(audioProcessor);
    audioProcessor.connect(audioContext.destination);

    console.log('Microphone started');
  } catch (err) {
    console.error('Mic error:', err);
    micStatus.textContent = '🎤 无权限';
  }
}

function float32ToPCM16(float32) {
  const buf = new ArrayBuffer(float32.length * 2);
  const view = new DataView(buf);
  for (let i = 0; i < float32.length; i++) {
    const s = Math.max(-1, Math.min(1, float32[i]));
    view.setInt16(i * 2, s < 0 ? s * 32768 : s * 32767, true);
  }
  return buf;
}

// ── 对话面板 ──────────────────────────────────────────────────────────
function addChatMessage(channel, text, videoTime) {
  // 清除占位文字
  const placeholder = chatMessages.querySelector('.chat-placeholder');
  if (placeholder) placeholder.remove();

  const timeStr = videoTime != null ? formatTime(videoTime) : '';

  const el = document.createElement('div');
  el.className = `msg ${channel}`;
  el.innerHTML = `
    <div class="msg-header">
      <span class="msg-tag ${channel}">${channelLabel(channel)}</span>
      <span>${timeStr}</span>
    </div>
    <div class="msg-body">${escapeHtml(text)}</div>
  `;
  chatMessages.appendChild(el);
  chatMessages.scrollTop = chatMessages.scrollHeight;
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
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function disconnectAll() {
  if (ws) { ws.close(); ws = null; }
  if (audioProcessor) { audioProcessor.disconnect(); audioProcessor = null; }
  if (audioContext) { audioContext.close(); audioContext = null; }
  if (mediaStream) { mediaStream.getTracks().forEach(t => t.stop()); mediaStream = null; }
  dotNitrogen.className = 'dot';
  dotVLM.className      = 'dot';
  ttsStatus.textContent = '🔇 待机';
  micStatus.textContent = '🎤 未连接';
}
