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
let lastNitrogenErrorMsg = '';
const WS_RECONNECT_BASE_MS = 1000;
const WS_RECONNECT_MAX_MS = 15000;
const APP_BUILD = '20250627-dual-voice';
let pcmSentCount = 0;
let asrEnabled = false;   // ASR 默认关闭
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
const chatFast      = $('chat-fast');
const chatSlow      = $('chat-slow');
const dotNitrogen   = $('dot-nitrogen');
const dotVLM        = $('dot-vlm');
const btnMute       = $('btn-mute');
const captureCanvas = $('capture-canvas');   // Fix 11
const captureCtx    = captureCanvas.getContext('2d');
const chkVideoAudio = $('chk-video-audio');
const voiceFast     = $('voice-fast');
const voiceSlow     = $('voice-slow');
const gameSelect    = $('game-select');

// ── 游戏列表（硬编码）─────────────────────────────────────────────────
// 4 个目标游戏写明确 game_id，其余保留展示名，id 用于 backend 词表查找
const GAME_LIST = [
  { id: "street_fighter_6",     label: "街头霸王6" },
  { id: "black_myth_wukong",    label: "黑神话：悟空" },
  { id: "new_super_mario_bros", label: "新超级马里奥兄弟" },
  { id: "forza_horizon_5",      label: "极限竞速：地平线5" },
  { id: "elden_ring",           label: "艾尔登法环" },
  { id: "sekiro",               label: "只狼：影逝二度" },
  { id: "genshin_impact",       label: "原神" },
  { id: "honkai_starrail",      label: "崩坏：星穹铁道" },
  { id: "pubg",                 label: "绝地求生" },
  { id: "naraka",               label: "永劫无间" },
  { id: "honor_of_kings",       label: "王者荣耀" },
  { id: "league_of_legends",    label: "英雄联盟" },
  { id: "dota2",                label: "DOTA 2" },
  { id: "cs2",                  label: "反恐精英2" },
  { id: "valorant",             label: "无畏契约" },
  { id: "overwatch2",           label: "守望先锋2" },
  { id: "apex_legends",         label: "Apex英雄" },
  { id: "warzone",              label: "使命召唤：战区" },
  { id: "diablo4",              label: "暗黑破坏神4" },
  { id: "monster_hunter_wilds", label: "怪物猎人：荒野" },
  { id: "dmc5",                 label: "鬼泣5" },
  { id: "god_of_war_ragnarok",  label: "战神：诸神黄昏" },
  { id: "ff16",                 label: "最终幻想16" },
  { id: "zelda_totk",           label: "塞尔达传说：王国之泪" },
  { id: "mario_wonder",         label: "超级马力欧：惊奇" },
  { id: "silksong",             label: "空洞骑士：丝之歌" },
  { id: "celeste",              label: "蔚蓝" },
  { id: "dead_cells",           label: "死亡细胞" },
  { id: "hades",                label: "哈迪斯" },
  { id: "hades2",               label: "哈迪斯2" },
  { id: "binding_of_isaac",     label: "以撒的结合" },
  { id: "slay_the_spire",       label: "杀戮尖塔" },
  { id: "bg3",                  label: "博德之门3" },
  { id: "cyberpunk2077",        label: "赛博朋克2077" },
  { id: "rdr2",                 label: "荒野大镖客2" },
  { id: "gta5",                 label: "侠盗猎车手5" },
  { id: "re4_remake",           label: "生化危机4 重制版" },
  { id: "sh2_remake",           label: "寂静岭2 重制版" },
  { id: "bloodborne",           label: "血源诅咒" },
  { id: "dark_souls3",          label: "黑暗之魂3" },
  { id: "nioh2",                label: "仁王2" },
  { id: "ghost_of_tsushima",    label: "对马岛之魂" },
  { id: "tlou2",                label: "最后生还者 Part II" },
  { id: "horizon_forbidden_west",label: "地平线：西之绝境" },
  { id: "stardew_valley",       label: "星露谷物语" },
  { id: "animal_crossing",      label: "动物森友会" },
  { id: "minecraft",            label: "我的世界" },
  { id: "terraria",             label: "泰拉瑞亚" },
  { id: "civ6",                 label: "文明6" },
  { id: "total_war_3k",         label: "全面战争：三国" },
  { id: "fire_emblem_3h",       label: "火焰纹章：风花雪月" },
  { id: "persona5_royal",       label: "女神异闻录5 皇家版" },
];
if (gameSelect) {
  gameSelect.innerHTML = '';
  for (const item of GAME_LIST) {
    const opt = document.createElement('option');
    opt.value = item.id;
    opt.textContent = item.label;
    gameSelect.appendChild(opt);
  }
}

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
    // 必须在用户手势有效期内先申请麦克风，否则 await 预热后 AudioContext 会挂起
    const micPrime = primeMicrophoneOnUserGesture().catch(err => {
      console.error('mic prime:', err);
      btnMute.textContent = '🎤 麦克风失败';
      btnMute.className = 'btn-mute error';
      throw err;
    });
    try {
      await micPrime;
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
      if (data.nitrogen_health && data.nitrogen_health.ok === false) {
        addSystemMsg(
          `NitroGen 远端未就绪：${data.nitrogen_health.message || '请检查 SSH 隧道与远端 FastAPI'}`
        );
      }

      isAnalysisRunning = true;
      if (data.asr_state) {
        updateMicStatus(data.asr_state);
      }
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
  chatFast.innerHTML = '';
  chatSlow.innerHTML = '';
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

if (voiceFast) {
  voiceFast.addEventListener('change', () => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'set_voice_fast', speaker: voiceFast.value }));
    }
  });
}

if (voiceSlow) {
  voiceSlow.addEventListener('change', () => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'set_voice_slow', speaker: voiceSlow.value }));
    }
  });
}

if (gameSelect) {
  gameSelect.addEventListener('change', () => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      const selectedId = gameSelect.value;
      const selectedItem = GAME_LIST.find(g => g.id === selectedId);
      ws.send(JSON.stringify({
        type: 'set_game',
        game_id: selectedId,
        game: selectedItem ? selectedItem.label : selectedId,
      }));
    }
  });
}

if (btnMute) {
  btnMute.addEventListener('click', () => {
    asrEnabled = !asrEnabled;
    syncMuteButton();
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'set_asr', enabled: asrEnabled }));
    }
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
function syncPrimaryStateToServer({ syncSeek = false } = {}) {
  if (!isPrimaryClient || !ws || ws.readyState !== WebSocket.OPEN) return;
  sendVideoReadyIfNeeded();
  // 暂停时也推一帧，供 VLM / NitroGen 使用
  captureSnapshotFrame();
  if (syncSeek) {
    isSeeking = true;
    ws.send(JSON.stringify({ type: 'seek', time: videoPlayer.currentTime }));
  }
  // 分析进行中一律 resume，避免误 pause 导致画面帧/ASR 链路卡住
  if (isAnalysisRunning) {
    ws.send(JSON.stringify({ type: 'playback', action: 'resume' }));
    return;
  }
  const shouldResume = !videoPlayer.paused && !videoPlayer.ended;
  ws.send(JSON.stringify({
    type: 'playback',
    action: shouldResume ? 'resume' : 'pause',
  }));
}

function setMicDisconnected() {
  asrEnabled = false;
  syncMuteButton();
}

function requestAsrState() {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'request_asr_state' }));
  }
}

async function applySessionRole(role) {
  const wasPrimary = isPrimaryClient;
  isPrimaryClient = (role === 'primary');
  if (isPrimaryClient) {
    await ensureMicrophoneReady();
    syncPrimaryStateToServer({ syncSeek: wasPrimary });
    startFrameCapture();
    requestAsrState();
    syncMuteButton();
    if (!wasPrimary) {
      addSystemMsg('已接管主连接');
    }
  } else {
    stopMicrophone();
    btnMute.textContent = '👁 旁观（只读）';
    btnMute.className = 'btn-mute muted';
    stopFrameCapture();
    stopTTSAudio();
  }
}

async function ensureMicrophoneReady() {
  if (!isPrimaryClient) return;
  try {
    await primeMicrophoneOnUserGesture();
    await attachMicPipeline();
    if (audioContext?.state === 'suspended') {
      await audioContext.resume();
    }
    requestAsrState();
  } catch (err) {
    console.error('ensureMicrophoneReady:', err);
    btnMute.textContent = '🎤 麦克风失败';
    btnMute.className = 'btn-mute error';
    addSystemMsg(`麦克风错误：${err.message}`);
  }
}

function connectWebSocket() {
  clearWsReconnectTimer();
  if (ws) {
    const old = ws;
    old.onclose = null;
    old.onerror = null;
    if (old.readyState === WebSocket.OPEN || old.readyState === WebSocket.CONNECTING) {
      old.close();
    }
  }
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(`${protocol}//${location.host}/ws`);
  ws.binaryType = 'arraybuffer';   // Fix 14：接收 ArrayBuffer 而非 Blob

  ws.onopen = () => {
    console.log('WebSocket connected (build %s)', APP_BUILD);
    pcmSentCount = 0;
    ws.send(JSON.stringify({ type: 'register', role: clientMode }));
    // 连接后立刻同步当前游戏选择到后端
    if (gameSelect && gameSelect.value) {
      const selectedId = gameSelect.value;
      const selectedItem = GAME_LIST.find(g => g.id === selectedId);
      ws.send(JSON.stringify({
        type: 'set_game',
        game_id: selectedId,
        game: selectedItem ? selectedItem.label : selectedId,
      }));
    }
  };

  ws.onclose = (ev) => {
    console.log('WebSocket closed', ev.code, ev.reason || '');
    stopFrameCapture();
    if (!isAnalysisRunning) {
      setMicDisconnected();
      return;
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
      applySessionRole(msg.role).catch(err => console.error('applySessionRole:', err));
      break;

    case 'primary_changed':
      addSystemMsg('主连接已切换');
      if (isPrimaryClient) {
        syncPrimaryStateToServer({ syncSeek: true });
      }
      break;

    case 'tts':
      if (msg.text) {
        const displayText = msg.channel === 'user' ? toSimplified(msg.text) : msg.text;
        addChatMessage(msg.channel, displayText, msg.video_time, msg.utterance_id);
      }
      if (msg.playing && msg.utterance_id != null) {
        pendingUtteranceId = msg.utterance_id;
      }
      break;

    case 'tts_interrupt':
      dismissUtterance(msg.utterance_id);
      if (msg.utterance_id == null
          || currentUtteranceId === msg.utterance_id
          || pendingUtteranceId === msg.utterance_id) {
        stopTTSAudio();
      }
      clearPlayingHighlight();
      break;

    case 'tts_end':
      clearPlayingHighlight();
      break;

    case 'asr_state':
      if (isPrimaryClient) updateMicStatus(msg.state);
      break;

    case 'vlm_state':
      dotVLM.className = msg.busy ? 'dot active' : 'dot';
      break;

    case 'nitrogen_state':
      if (msg.status === 'ok' || (msg.inference_count > 0 && !msg.last_error)) {
        if (dotNitrogen.className !== 'dot active') {
          dotNitrogen.className = 'dot active';
        }
        dotNitrogen.title = msg.message || `NitroGen 已连接（推理 ${msg.inference_count || 0} 次）`;
      } else if (msg.status === 'error' || msg.last_error) {
        dotNitrogen.className = 'dot error';
        const errText = msg.message || msg.last_error || 'NitroGen 连接失败';
        dotNitrogen.title = errText;
        if (errText !== lastNitrogenErrorMsg) {
          lastNitrogenErrorMsg = errText;
          addSystemMsg(`NitroGen：${errText}`);
        }
      } else {
        dotNitrogen.className = 'dot loading';
        dotNitrogen.title = 'NitroGen：等待首帧/首次推理';
      }
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
      dotNitrogen.title = `NitroGen：${msg.intent} @ ${msg.video_time}s`;
      break;

    case 'seek_done':
      isSeeking = false;
      stopTTSAudio();
      break;

    case 'status':
      if (msg.state === 'started') {
        dotNitrogen.className = 'dot loading';
        dotNitrogen.title = 'NitroGen：等待首帧/首次推理';
      }
      if (msg.state === 'video_ended_can_ask' && msg.message) {
        addSystemMsg(msg.message);
      }
      if (msg.state === 'user_question_no_frame') {
        addSystemMsg(msg.message || '画面未就绪，暂时无法回答，请点击播放视频后再问');
      }
      if (msg.state === 'vlm_error' && msg.message) {
        addSystemMsg(msg.message);
      }
      break;

    case 'request_frame':
      captureSnapshotFrame();
      break;

    case 'video_ended':
      addSystemMsg('视频播放结束，仍可继续语音提问');
      requestAsrState();
      ensureMicrophoneReady().catch(() => {});
      break;

    case 'conversation_cleared':
      chatFast.innerHTML = '';
      chatSlow.innerHTML = '';
      break;

    case 'voice_error':
      addSystemMsg(`音色切换失败：${msg.message || '该音色不可用，已恢复原音色'}`);
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

function sendJpegFrame(jpegBuf, videoTime) {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  const header = new ArrayBuffer(9);
  const view = new DataView(header);
  view.setUint8(0, 0x02);
  view.setFloat64(1, videoTime, true);
  const msg = new Uint8Array(9 + jpegBuf.byteLength);
  msg.set(new Uint8Array(header), 0);
  msg.set(new Uint8Array(jpegBuf), 9);
  ws.send(msg.buffer);
}

/** 推送当前画面一帧（视频暂停时也可用，供 VLM / NitroGen） */
function captureSnapshotFrame() {
  if (!isPrimaryClient || !ws || ws.readyState !== WebSocket.OPEN) return;
  if (videoPlayer.readyState < 2) return;
  captureCtx.drawImage(videoPlayer, 0, 0, 256, 256);
  captureCanvas.toBlob(blob => {
    if (!blob || !ws || ws.readyState !== WebSocket.OPEN) return;
    blob.arrayBuffer().then(jpegBuf => {
      sendJpegFrame(jpegBuf, videoPlayer.currentTime);
    });
  }, 'image/jpeg', 0.85);
}

function captureAndSendFrame() {
  if (!isPrimaryClient || !ws || ws.readyState !== WebSocket.OPEN) return;
  if (videoPlayer.paused || videoPlayer.ended || videoPlayer.readyState < 2) return;

  captureCtx.drawImage(videoPlayer, 0, 0, 256, 256);

  captureCanvas.toBlob(blob => {
    if (!blob || !ws || ws.readyState !== WebSocket.OPEN) return;
    blob.arrayBuffer().then(jpegBuf => {
      sendJpegFrame(jpegBuf, videoPlayer.currentTime);
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

/** 在用户点击瞬间申请权限并激活 AudioContext（避免 await 后手势失效） */
async function primeMicrophoneOnUserGesture() {
  if (!navigator.mediaDevices?.getUserMedia) {
    throw new Error('当前浏览器不支持麦克风');
  }
  if (!mediaStream) {
    mediaStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: false,
        autoGainControl: true,
      },
    });
  }
  if (!audioContext || audioContext.state === 'closed') {
    audioContext = new AudioContext();
  }
  if (audioContext.state === 'suspended') {
    await audioContext.resume();
  }
  if (audioContext.state !== 'running') {
    throw new Error(`AudioContext 未激活 (${audioContext.state})`);
  }
}

function sendPcmToServer(samples) {
  if (!asrEnabled) return;
  if (!isPrimaryClient || !ws || ws.readyState !== WebSocket.OPEN) return;
  const pcm16 = float32ToPCM16(samples);
  const msg = new Uint8Array(1 + pcm16.byteLength);
  msg[0] = 0x01;
  msg.set(new Uint8Array(pcm16), 1);
  ws.send(msg.buffer);
  pcmSentCount += 1;
  if (pcmSentCount === 1) {
    console.log('PCM: first chunk sent', pcm16.byteLength, 'bytes');
  } else if (pcmSentCount % 100 === 0) {
    console.log('PCM:', pcmSentCount, 'chunks sent');
  }
}

async function attachMicPipeline() {
  if (!mediaStream || !audioContext) {
    throw new Error('麦克风未初始化');
  }
  if (audioContext.state === 'suspended') {
    await audioContext.resume();
  }
  if (audioProcessor) {
    return;
  }

  const source = audioContext.createMediaStreamSource(mediaStream);
  const micGain = audioContext.createGain();
  micGain.gain.value = 3.0;
  const frameSize = Math.round(audioContext.sampleRate * 0.1);
  const workletUrl = new URL('/static/pcm-processor.worklet.js', location.origin);
  workletUrl.searchParams.set('v', APP_BUILD);

  if (audioContext.audioWorklet) {
    await audioContext.audioWorklet.addModule(workletUrl.href);
    audioProcessor = new AudioWorkletNode(audioContext, 'pcm-processor', {
      processorOptions: {
        frameSize,
        targetSampleRate: 16000,
      },
    });
    audioProcessor.port.onmessage = e => {
      const samples = e.data instanceof Float32Array
        ? e.data
        : new Float32Array(e.data);
      if (!samples.length) return;
      sendPcmToServer(samples);
    };
    const silentGain = audioContext.createGain();
    silentGain.gain.value = 0;
    source.connect(micGain);
    micGain.connect(audioProcessor);
    audioProcessor.connect(silentGain);
    silentGain.connect(audioContext.destination);
  } else {
    startMicrophoneScriptProcessor(source, micGain, frameSize);
  }
}

async function startMicrophone() {
  try {
    await primeMicrophoneOnUserGesture();
    await attachMicPipeline();
    console.log('Microphone started @', audioContext.sampleRate, 'Hz, build', APP_BUILD);
    requestAsrState();
  } catch (err) {
    console.error('Mic error:', err);
    btnMute.textContent = '🎤 无权限';
    btnMute.className = 'btn-mute error';
    throw err;
  }
}

/** ScriptProcessor 回退（旧浏览器）；不将麦克风路由到扬声器。 */
function startMicrophoneScriptProcessor(source, micGain, frameSize) {
  const bufferSize = Math.max(256, Math.min(16384, frameSize));
  audioProcessor = audioContext.createScriptProcessor(bufferSize, 1, 1);

  audioProcessor.onaudioprocess = e => {
    if (!isPrimaryClient || !ws || ws.readyState !== WebSocket.OPEN) return;
    const float32 = e.inputBuffer.getChannelData(0);
    sendPcmToServer(
      audioContext.sampleRate === 16000
        ? float32
        : resampleFloat32(float32, audioContext.sampleRate, 16000),
    );
  };

  const silentGain = audioContext.createGain();
  silentGain.gain.value = 0;
  source.connect(micGain);
  micGain.connect(audioProcessor);
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

// ── 繁体→简体 常用字映射 ──────────────────────────────────────────────
const T2S_MAP = '東东車车門门馬马風风龍龙鳥鸟魚鱼學学書书電电飛飞語语說说話话買买開开關关點点頭头聽听見见問问題题讓让還还機机時时會会對对從从過过這这裡里後后單单員员區区實实際际經经統统處处國国報报業业資资現现環环體体發发產产總总進进運运選选達达邊边連连動动應应種种數数與与無无場场變变聯联腦脑離离難难觀观歲岁則则參参決决結结組组當当將将歡欢傳传豐丰準准極极護护齊齐廣广億亿鐵铁隨随優优農农給给輸输親亲線线節节辦办調调傑杰聲声壓压歷历構构響响權权築筑廳厅競竞義义確确標标據据條条築筑費费勢势質质監监損损試试鮮鲜隊队覺觉專专戰战練练層层認认師师較较論论穩稳許许創创評评際际協协識识驗验歸归養养燈灯燒烧壞坏頻频編编擊击護护獨独額额術术閱阅齡龄遠远複复屬属導导齒齿藝艺類类記记設设訓训雜杂輕轻紀纪貴贵軍军覆复範范構构貿贸幣币圖图銀银獎奖簡简衝冲績绩療疗帶带塊块歲岁陽阳歐欧殘残薦荐壇坛華华慶庆寶宝濟济嗎吗劇剧鄰邻搶抢闆板蘭兰禮礼紅红壽寿衛卫兩两個个網网張张長长開开頁页幫帮'.split('');
const _t2s = {};
for (let i = 0; i < T2S_MAP.length; i += 2) {
  _t2s[T2S_MAP[i]] = T2S_MAP[i + 1];
}
function toSimplified(text) {
  let out = '';
  for (const ch of text) out += _t2s[ch] || ch;
  return out;
}

// ── 对话面板 ──────────────────────────────────────────────────────────
function addChatMessage(channel, text, videoTime, utteranceId) {
  const isFast = (channel === 'fast');
  const container = isFast ? chatFast : chatSlow;

  const placeholder = container.querySelector('.chat-placeholder');
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
  container.appendChild(el);
  container.scrollTop = container.scrollHeight;
  return el;
}

function highlightPlayingMessage(utteranceId) {
  clearPlayingHighlight();
  if (utteranceId == null) return;
  const el = chatFast.querySelector(`[data-utterance-id="${utteranceId}"]`)
          || chatSlow.querySelector(`[data-utterance-id="${utteranceId}"]`);
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
  if (!isPrimaryClient || !asrEnabled) return;
  const labels = {
    listening:  '🎤 收音中',
    recording:  '🎤● 正在说话',
    processing: '🎤 识别中…',
    muted:      '🎤 收音中',
  };
  btnMute.textContent = labels[state] || '🎤 收音中';
  btnMute.className   = 'btn-mute active';
}

function syncMuteButton() {
  btnMute.textContent = asrEnabled ? '🎤 收音中' : '🔇 静音';
  btnMute.className   = asrEnabled ? 'btn-mute active' : 'btn-mute muted';
}

function addSystemMsg(text) {
  const el = document.createElement('div');
  el.style.cssText = 'color:#64748b;font-size:12px;text-align:center;padding:4px 0';
  el.textContent = text;
  chatSlow.appendChild(el);
  chatSlow.scrollTop = chatSlow.scrollHeight;
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
  btnMute.textContent = '👁 旁观';

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
  btnMute.textContent = '👁 旁观';
  addSystemMsg('已断开旁观连接');
}

function disconnectAll() {
  isAnalysisRunning = false;
  clearWsReconnectTimer();
  stopFrameCapture();
  stopTTSAudio();
  dismissedUtteranceIds.clear();
  pcmSentCount = 0;
  if (ws)              { ws.close(); ws = null; }
  stopMicrophone();
  dotNitrogen.className = 'dot';
  dotVLM.className      = 'dot';
  setMicDisconnected();
}
