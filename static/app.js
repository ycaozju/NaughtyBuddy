const $ = (id) => document.getElementById(id);

// The server protocol is kept here so that transport and UI stay separate.
const protocol = {
  async request(path, method = 'GET', body) {
    const response = await fetch(`/api${path}`, {
      method, credentials: 'same-origin', cache: 'no-store',
      headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    let value = {};
    try { value = await response.json(); } catch { /* Empty successful responses are valid. */ }
    if (!response.ok) {
      const error = new Error(typeof value.detail === 'string' ? value.detail : value.message || '操作未完成，请稍后再试。');
      error.status = response.status;
      throw error;
    }
    return value;
  },
  me: () => protocol.request('/me'),
  login: (code) => protocol.request('/auth/login', 'POST', { code }),
  parent: (pin) => protocol.request('/auth/parent', 'POST', { pin }),
  logout: () => protocol.request('/auth/logout', 'POST'),
  lock: () => protocol.request('/auth/lock', 'POST'),
  pause: () => protocol.request('/admin/pause', 'POST'),
  settings: () => protocol.request('/settings'),
  saveSettings: (settings) => protocol.request('/settings', 'PATCH', settings),
  memory: () => protocol.request('/memory'),
  saveMemory: (memory) => protocol.request('/memory', 'PUT', memory),
  deleteMemory: () => protocol.request('/memory', 'DELETE'),
  revoke: () => protocol.request('/admin/revoke', 'POST'),
  socket: () => new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/ws`),
};

const labels = {
  idle: ['给小芽打个电话吧', '点一次拨打，接通后就能一直说话。'],
  connecting: ['正在接通小芽', '正在准备麦克风和语音连接。'],
  listening: ['我在听，你慢慢说', '想一想也没关系，说完后等一小会儿。'],
  transcribing: ['让我听清楚', '你的话，我正在仔细听。'],
  thinking: ['让我想一想', '一个小小的想法正在发芽。'],
  checking: ['马上就说给你听', '再等一小会儿。'],
  speaking: ['小芽正在说', '想插话时，直接说就好。'],
  ended: ['这次先玩到这里', '去伸伸懒腰，找大人聊聊刚才的故事吧。'],
};
const state = {
  auth: { authenticated: false, role: 'child' }, provider: 'local', config: { session_minutes: 10, daily_minutes: 30, vad_silence_ms: 1400, allowed_start_hour: 7, allowed_end_hour: 21 },
  memory: { enabled: false, preferences: [] }, preferenceOptions: ['恐龙', '小动物', '森林', '太空', '海洋', '汽车', '音乐', '画画', '兔子', '猫咪'], dailyUsed: 0, mode: 'story', active: false, starting: false,
  generation: 0, socket: null, stream: null, context: null, recorder: null, source: null,
  heartbeat: null, clock: null, deadline: 0, startedAt: 0, currentTurn: null, ignoredTurns: new Set(),
  queue: [], playback: null, playbackGeneration: 0, playing: false, uiState: 'idle',
  preroll: [], frames: [], speech: false, voicedFrames: 0, silentFrames: 0,
  onsetFrames: 0, onsetWindow: [], noiseFloor: .003, ptt: false, voiceMode: 'continuous', muted: false, captureVersion: 0, misses: 0,
  selectedActivities: [...document.querySelectorAll('[data-mode]')], warningShown: false,
};

function notice(message = '', isError = false) {
  $('notice').textContent = message;
  $('notice').hidden = !message;
  $('notice').classList.toggle('error', isError);
}
function parentMessage(message = '', isError = false) {
  $('parent-message').textContent = message;
  $('parent-message').classList.toggle('error', isError);
}
function setStatus(name, detail) {
  state.uiState = name;
  let [title, description] = labels[name] || labels.listening;
  if (state.active && name === 'listening') {
    if (state.muted) { title = '麦克风已静音'; description = '小芽听不到你，取消静音后再继续。'; }
    else if (state.voiceMode === 'ptt' && !state.ptt) { title = '按住按钮，再说一句'; description = '松开后发送，也可以切回连续通话。'; }
  }
  if (state.active && name === 'speaking') {
    if (state.muted) description = '麦克风已静音，你仍然可以听小芽说话。';
    else if (state.voiceMode === 'ptt') description = '想插话时，按住下面的说话按钮。';
  }
  $('status-title').textContent = title;
  $('status-detail').textContent = state.muted && name === 'listening' ? description : detail || description;
  $('scene').dataset.state = state.muted && name === 'listening' ? 'muted' : name;
}
function updateControls() {
  const ready = state.active;
  $('start-button').disabled = ready || state.starting;
  $('start-button').querySelector('span').textContent = state.starting ? '接通中…' : ready ? '通话中' : '拨打小芽';
  $('stop-button').disabled = !ready && !state.starting;
  $('mute-button').disabled = !ready;
  $('mute-button').textContent = state.muted ? '取消静音' : '静音';
  $('mute-button').setAttribute('aria-pressed', String(state.muted));
  $('mute-button').classList.toggle('active', state.muted);
  $('ptt-button').disabled = !ready || state.muted;
  $('ptt-row').hidden = state.voiceMode !== 'ptt';
  for (const mode of ['continuous', 'ptt']) {
    const button = $(`${mode}-mode`);
    button.disabled = state.starting;
    button.classList.toggle('active', state.voiceMode === mode);
    button.setAttribute('aria-pressed', String(state.voiceMode === mode));
  }
  $('call-state').textContent = state.starting ? '接通中' : ready ? state.muted ? '通话中 · 已静音' : '通话中' : '尚未接通';
  $('call-status').classList.toggle('connected', ready);
  $('call-hint').textContent = state.voiceMode === 'continuous' ? '接通后直接说话，不用每句按按钮；想插话时也可以直接说。' : '按住下面的按钮说话，松开后发送。可随时切回连续通话。';
  $('text-input').disabled = !ready;
  $('send-button').disabled = !ready;
  state.selectedActivities.forEach((button) => { button.disabled = ready || state.starting; });
  const listening = ready && !!state.stream && !state.muted && (state.voiceMode === 'continuous' || state.ptt);
  $('mic-indicator').classList.toggle('on', listening);
  $('mic-indicator').lastChild.textContent = ready && state.muted ? '麦克风已静音' : listening ? '麦克风已开启' : ready && state.voiceMode === 'ptt' ? '按住时开启麦克风' : '麦克风已关闭';
}
function updateTime() {
  const elapsed = state.active ? Math.max(0, Math.floor((Date.now() - state.startedAt) / 1000)) : 0;
  $('call-time').textContent = `${String(Math.floor(elapsed / 60)).padStart(2, '0')}:${String(elapsed % 60).padStart(2, '0')}`;
  if (!state.active) {
    $('time-label').textContent = `每次 ${state.config.session_minutes} 分钟`;
    return;
  }
  const remaining = Math.max(0, Math.ceil((state.deadline - Date.now()) / 1000));
  $('time-label').textContent = `还可以玩 ${Math.floor(remaining / 60)}:${String(remaining % 60).padStart(2, '0')}`;
  if (remaining <= 60 && remaining > 0 && !state.warningShown) {
    state.warningShown = true;
    notice('还有 1 分钟。这一小段聊完，就休息一下吧。');
  }
  if (!remaining) stopSession('时间到了，去和大人一起活动一下吧。', 'ended');
}
function send(message) {
  if (state.socket?.readyState === WebSocket.OPEN) {
    state.socket.send(typeof message === 'object' && !(message instanceof ArrayBuffer) ? JSON.stringify(message) : message);
    return true;
  }
  return false;
}
function addBubble(speaker, text, turnId) {
  const container = $('conversation');
  container.querySelector('.empty-conversation')?.remove();
  const row = document.createElement('div');
  row.className = `bubble ${speaker}`;
  if (turnId !== undefined && turnId !== null) row.dataset.turn = String(turnId);
  const name = document.createElement('span');
  name.className = 'bubble-speaker';
  name.textContent = speaker === 'child' ? '你' : '小芽';
  const content = document.createElement('p');
  content.className = 'bubble-text';
  content.textContent = text;
  row.append(name, content);
  container.append(row);
  while (container.children.length > 12) container.firstElementChild.remove();
  container.scrollTop = container.scrollHeight;
  return row;
}
function resetVAD() {
  state.preroll = [];
  state.frames = [];
  state.speech = false;
  state.voicedFrames = 0;
  state.silentFrames = 0;
  state.onsetFrames = 0;
  state.onsetWindow = [];
}
function syncMicrophone() {
  // Gate both the device track and the worklet. Stale queued frames from before
  // a mute/mode change carry an old version and cannot become a later utterance.
  const enabled = state.active && !state.muted && (state.voiceMode === 'continuous' || state.ptt) && !document.hidden;
  state.captureVersion++;
  const tracks = state.stream?.getAudioTracks?.() || state.stream?.getTracks?.() || [];
  tracks.forEach((track) => { track.enabled = enabled; });
  state.recorder?.port.postMessage?.({ type: 'capture', enabled, version: state.captureVersion });
}
function setVoiceMode(mode) {
  if (!['continuous', 'ptt'].includes(mode) || state.starting || mode === state.voiceMode) return;
  releasePTT(false);
  resetVAD();
  state.voiceMode = mode;
  syncMicrophone();
  notice();
  updateControls();
  setStatus(state.uiState);
}
function toggleMute() {
  if (!state.active) return;
  state.muted = !state.muted;
  releasePTT(false);
  resetVAD();
  syncMicrophone();
  updateControls();
  setStatus(state.uiState);
}
function stopAudio(interrupted = true) {
  state.playbackGeneration++;
  state.queue = [];
  if (state.playback) {
    const { source, bubble } = state.playback;
    source.onended = null;
    try { source.stop(); } catch { /* Already stopped. */ }
    source.disconnect();
    if (interrupted && bubble) bubble.classList.add('partial');
    state.playback = null;
  }
  state.playing = false;
}
function interrupt() {
  if (!state.active) return;
  if (state.currentTurn !== null) {
    state.ignoredTurns.add(String(state.currentTurn));
    if (state.ignoredTurns.size > 100) state.ignoredTurns.delete(state.ignoredTurns.values().next().value);
  }
  stopAudio(); // Stop locally before waiting for any network acknowledgement.
  send({ type: 'interrupt' });
  setStatus('listening', '你继续说，我在听。');
}
function isRetired(turnId) {
  return turnId !== undefined && turnId !== null && state.ignoredTurns.has(String(turnId));
}
async function playNext() {
  if (state.playback || !state.queue.length || !state.active || !state.context) return;
  const chunk = state.queue.shift();
  if (isRetired(chunk.turn_id) || chunk.safety !== 'approved') { void playNext(); return; }
  const generation = state.playbackGeneration;
  // Reserve the playback slot during asynchronous decoding to preserve chunk order.
  state.playback = { source: { stop() {}, disconnect() {}, onended: null }, bubble: null };
  try {
    const encoded = atob(chunk.audio_base64);
    const bytes = Uint8Array.from(encoded, (char) => char.charCodeAt(0));
    const context = state.context;
    const audio = await context.decodeAudioData(bytes.buffer);
    if (generation !== state.playbackGeneration || !state.active || isRetired(chunk.turn_id)) return;
    const source = context.createBufferSource();
    source.buffer = audio;
    source.connect(context.destination);
    const bubble = chunk.text ? addBubble('assistant', chunk.text, chunk.turn_id) : null;
    state.playback = { source, bubble };
    state.playing = true;
    setStatus('speaking');
    source.onended = () => {
      source.disconnect();
      if (generation !== state.playbackGeneration || !state.active) return;
      state.playback = null;
      state.playing = false;
      // A complete chunk is acknowledged only after browser playback actually ends.
      send({ type: 'played', turn_id: chunk.turn_id, chunk_id: chunk.chunk_id });
      if (state.queue.length) void playNext();
      else setStatus('listening');
    };
    source.start();
  } catch {
    if (generation !== state.playbackGeneration) return;
    state.playback = null;
    state.playing = false;
    notice('刚才的声音没能播放。请让大人检查音量，或再试一次。', true);
    setStatus('listening');
    if (state.queue.length) void playNext();
  }
}
function submitSpeech() {
  const frames = state.frames;
  const voicedFrames = state.voicedFrames;
  resetVAD();
  if (!state.active || state.muted || document.hidden || !frames.length) return;
  if (voicedFrames < 8) {
    if (state.voiceMode === 'ptt') notice('这次还没有听清，按住按钮后慢慢说一句吧。');
    return;
  }
  const samples = new Int16Array(frames.reduce((size, frame) => size + frame.length, 0));
  let offset = 0;
  for (const frame of frames) { samples.set(frame, offset); offset += frame.length; }
  if (send(samples.buffer)) setStatus('transcribing');
  else stopSession('连接断开了，请重新开始。', 'idle');
}
function captureFrame({ pcm, rms, version }) {
  if (!state.active || state.muted || document.hidden || (version !== undefined && version !== state.captureVersion)) return;
  const samples = new Int16Array(pcm);
  if (state.voiceMode === 'ptt' && !state.ptt) return;
  const isBusy = !!state.playback || state.queue.length > 0 || ['transcribing', 'thinking', 'checking', 'speaking'].includes(state.uiState);
  const threshold = Math.max(state.playing ? .022 : .014, state.noiseFloor * (state.playing ? 5 : 3));
  const voiced = rms > threshold;
  if (!state.speech && !voiced && !state.playing) state.noiseFloor = Math.min(.013, state.noiseFloor * .98 + rms * .02);
  if (!state.speech) {
    state.preroll.push(samples);
    if (state.preroll.length > 15) state.preroll.shift();
    state.onsetFrames = voiced ? state.onsetFrames + 1 : 0;
    state.onsetWindow.push(voiced);
    if (state.onsetWindow.length > 15) state.onsetWindow.shift();
    const windowVoicedFrames = state.onsetWindow.filter(Boolean).length;
    // Mandarin syllables contain short energy dips. Require 200ms of voice
    // within 300ms rather than an uninterrupted loud 240ms during playback.
    const onset = state.playing ? voiced && windowVoicedFrames >= 10 : state.onsetFrames >= 3;
    if (state.ptt || onset) {
      if (isBusy) interrupt();
      state.speech = true;
      state.frames = state.preroll.slice();
      state.preroll = [];
      state.voicedFrames = Math.max(state.onsetFrames, windowVoicedFrames);
      state.silentFrames = 0;
    }
    return;
  }
  state.frames.push(samples);
  if (voiced) { state.voicedFrames++; state.silentFrames = 0; }
  else state.silentFrames++;
  const maxFrames = 900; // 18 seconds leaves room below the server's 20-second limit.
  if (state.frames.length >= maxFrames || (!state.ptt && state.silentFrames * 20 >= state.config.vad_silence_ms)) {
    if (state.frames.length >= maxFrames) notice('这一句先说到这里。小芽听完后，你可以继续说。');
    submitSpeech();
    if (state.ptt) releasePTT(false);
  }
}
async function setupMicrophone(generation) {
  if (!navigator.mediaDevices?.getUserMedia || !window.AudioWorkletNode) {
    throw new Error('这个浏览器还不能使用语音。请用更新的浏览器，并通过安全连接打开。');
  }
  const stream = await navigator.mediaDevices.getUserMedia({ audio: {
    channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true,
  }, video: false });
  if (generation !== state.generation || document.hidden) { stream.getTracks().forEach((track) => track.stop()); return false; }
  state.stream = stream;
  syncMicrophone();
  const Context = window.AudioContext || window.webkitAudioContext;
  const context = new Context({ sampleRate: 16000, latencyHint: 'interactive' });
  state.context = context;
  await context.resume();
  await context.audioWorklet.addModule('/static/pcm-worklet.js');
  if (generation !== state.generation || document.hidden) { stream.getTracks().forEach((track) => track.stop()); await context.close(); return false; }
  const source = context.createMediaStreamSource(stream);
  const recorder = new AudioWorkletNode(context, 'pcm-recorder');
  recorder.port.onmessage = (event) => captureFrame(event.data);
  source.connect(recorder);
  recorder.connect(context.destination);
  state.source = source;
  state.recorder = recorder;
  syncMicrophone();
  stream.getAudioTracks().forEach((track) => track.addEventListener('ended', () => {
    if (state.active) stopSession('麦克风连接已停止，请重新开始。', 'idle');
  }, { once: true }));
  return true;
}
async function startSession() {
  if (state.active || state.starting || document.hidden) return;
  if (!state.auth.authenticated) { openParent(); parentMessage('请先由大人输入家庭口令。'); return; }
  const generation = ++state.generation;
  state.starting = true;
  state.warningShown = false;
  state.misses = 0;
  state.muted = false;
  state.startedAt = 0;
  resetVAD();
  state.currentTurn = null;
  state.ignoredTurns.clear();
  state.noiseFloor = .003;
  $('ptt-mode').classList.remove('recommended');
  $('ptt-hint').textContent = '也可以按住按钮，松开后发送';
  notice();
  setStatus('connecting');
  updateControls();
  try {
    if (!await setupMicrophone(generation)) return;
    const socket = protocol.socket();
    state.socket = socket;
    const connectTimeout = setTimeout(() => {
      if (generation === state.generation && !state.active) stopSession('小芽还没有准备好，请让大人检查服务。', 'idle');
    }, 12000);
    socket.addEventListener('open', () => {
      if (generation !== state.generation) { socket.close(); return; }
      send({ type: 'start', mode: state.mode });
    });
    socket.addEventListener('message', (event) => {
      if (generation !== state.generation) return;
      try {
        const message = JSON.parse(event.data);
        if (message.type === 'session_started') clearTimeout(connectTimeout);
        handleMessage(message);
      } catch { stopSession('连接出现了一点问题，请重新开始。', 'idle'); }
    });
    socket.addEventListener('close', () => {
      clearTimeout(connectTimeout);
      if (generation === state.generation) stopSession('连接已结束。准备好时，可以重新开始。', 'idle');
    });
    socket.addEventListener('error', () => {
      clearTimeout(connectTimeout);
      if (generation === state.generation) stopSession('暂时连接不上小芽，请让大人检查服务。', 'idle');
    });
  } catch (error) {
    if (generation !== state.generation) return;
    const message = error.name === 'NotAllowedError' ? '需要允许麦克风才能说话。请让大人在浏览器里开启麦克风权限。' :
      error.name === 'NotFoundError' ? '没有找到麦克风，请让大人连接麦克风后重试。' :
      error.name === 'NotReadableError' ? '麦克风正在被其他应用使用，请让大人检查后重试。' :
      error.message?.startsWith('这个浏览器') ? error.message : '语音没有准备好，请让大人检查麦克风和浏览器。';
    stopSession(message, 'idle');
  }
}
function stopSession(message = '', status = 'ended') {
  state.generation++;
  send({ type: 'stop' });
  state.active = false;
  state.starting = false;
  state.muted = false;
  state.startedAt = 0;
  clearInterval(state.heartbeat);
  clearInterval(state.clock);
  state.heartbeat = null;
  state.clock = null;
  stopAudio();
  resetVAD();
  releasePTT(false);
  if (state.recorder) { state.recorder.port.onmessage = null; state.recorder.disconnect(); state.recorder = null; }
  if (state.source) { state.source.disconnect(); state.source = null; }
  if (state.stream) { state.stream.getTracks().forEach((track) => track.stop()); state.stream = null; }
  if (state.context) { const context = state.context; state.context = null; if (context.state !== 'closed') void context.close().catch(() => {}); }
  if (state.socket) { const socket = state.socket; state.socket = null; if (socket.readyState < WebSocket.CLOSING) socket.close(1000, 'Client stopped'); }
  setStatus(status);
  updateControls();
  updateTime();
  if (message) notice(message);
}
function handleMessage(message) {
  switch (message.type) {
    case 'session_started': {
      state.starting = false;
      state.active = true;
      state.startedAt = Date.now();
      const remaining = Number(message.remaining_seconds ?? message.limit_seconds ?? state.config.session_minutes * 60);
      state.deadline = Date.now() + Math.max(0, remaining) * 1000;
      state.heartbeat = setInterval(() => send({ type: 'ping' }), 5000);
      state.clock = setInterval(updateTime, 1000);
      syncMicrophone();
      setStatus('listening');
      updateControls();
      updateTime();
      break;
    }
    case 'pong':
      if (Number.isFinite(message.remaining_seconds)) state.deadline = Date.now() + Math.max(0, message.remaining_seconds) * 1000;
      updateTime();
      break;
    case 'state':
      if (isRetired(message.turn_id)) break;
      if (message.turn_id !== undefined && message.turn_id !== null) state.currentTurn = message.turn_id;
      if (state.active && !state.playing && !state.speech) setStatus(message.state);
      break;
    case 'transcript':
      if (isRetired(message.turn_id)) break;
      if (message.turn_id !== undefined && message.turn_id !== null) state.currentTurn = message.turn_id;
      if (message.text?.trim()) { addBubble('child', message.text, message.turn_id); state.misses = 0; }
      break;
    case 'greeting':
    case 'audio':
      if (message.type === 'greeting' && (state.speech || state.ptt || (state.currentTurn !== null && String(state.currentTurn) !== String(message.turn_id)))) break;
      if (!state.active || isRetired(message.turn_id)) break;
      if (message.category === 'unclear') recordUnclear();
      if (message.safety !== 'approved' || message.mime !== 'audio/wav') {
        notice('这一句暂时不能播放，我们换个话题吧。');
        break;
      }
      state.currentTurn = message.turn_id;
      state.queue.push(message);
      void playNext();
      break;
    case 'interrupted': {
      const canceled = message.canceled_turn_id ?? message.turn_id;
      if (canceled !== undefined && canceled !== null) state.ignoredTurns.add(String(canceled));
      // A delayed acknowledgement must not stop audio from a newer turn.
      if (String(canceled) === String(state.currentTurn)) {
        stopAudio();
        if (state.active) setStatus('listening');
      }
      break;
    }
    case 'error': {
      const code = String(message.code || '').toLowerCase();
      if (/empty|unclear|no_speech|asr|recognition/.test(code)) {
        recordUnclear();
      } else if (/limit|budget|quota|daily/.test(code)) {
        stopSession('今天先玩到这里。想继续安排时间，请找大人。', 'ended');
        break;
      } else if (/auth|unauthorized|forbidden/.test(code)) {
        stopSession('请大人重新连接这个设备。', 'idle');
        state.auth.authenticated = false;
        void loadMe();
        break;
      } else notice('小芽刚才没有接上。可以再试一句，或请大人帮忙。', true);
      if (message.retryable === false) stopSession('小芽需要休息一下。请让大人检查后再开始。', 'idle');
      else if (state.active) setStatus('listening');
      break;
    }
    case 'ended': {
      const reason = String(message.reason || '');
      const detail = reason === 'outside_hours' ? '现在是休息时间。请在大人安排的时段再来玩。' : reason === 'parent_pause' ? '大人请大家休息一下，去看看真实的世界吧。' : reason === 'voice_unavailable' ? '这个声音暂时无法使用，电话已挂断。请大人在家长空间重新选择音色。' : reason === 'voice_deleted' ? '这个音色已被大人删除，电话已挂断。选好新的声音后可以重新拨打。' : /limit|time|daily|budget/.test(reason) ? '时间到了，去和大人一起活动一下吧。' : '这次对话已经结束。准备好时，可以重新开始。';
      stopSession(detail, 'ended');
      break;
    }
  }
}
function recordUnclear() {
  state.misses++;
  if (state.misses >= 2) {
    $('ptt-mode').classList.add('recommended');
    notice(state.voiceMode === 'continuous' ? '刚才没有听清，电话还接通着。你可以继续慢慢说，也可以自己切换“按住说话”。' : '刚才没有听清。按住按钮慢慢说，也可以用键盘。');
  } else notice('刚才没有听清，你可以慢慢再说一次。');
}
function startPTT(event) {
  if (!state.active || state.muted || state.voiceMode !== 'ptt' || state.ptt) return;
  if (event?.type === 'pointerdown') { event.preventDefault(); event.currentTarget.setPointerCapture?.(event.pointerId); }
  if (state.playback || state.queue.length || ['thinking', 'speaking', 'checking', 'transcribing'].includes(state.uiState)) interrupt();
  resetVAD();
  state.ptt = true;
  syncMicrophone();
  updateControls();
  $('ptt-button').classList.add('pressed');
  $('ptt-button').textContent = '正在听 · 松开发送';
  setStatus('listening', '按住按钮，慢慢说。');
}
function releasePTT(submit = true) {
  if (!state.ptt) return;
  state.ptt = false;
  $('ptt-button').classList.remove('pressed');
  $('ptt-button').textContent = '按住说话';
  if (submit) submitSpeech();
  syncMicrophone();
  updateControls();
  if (state.active && state.uiState === 'listening') setStatus('listening');
}
function syncParent() {
  const authenticated = !!state.auth.authenticated;
  const parent = authenticated && state.auth.role === 'parent';
  $('login-panel').hidden = authenticated;
  $('parent-unlock-panel').hidden = !authenticated || parent;
  $('settings-panel').hidden = !parent;
  window.babybotParentUnlocked = parent;
  window.babybotVoiceSettings?.setUnlocked(parent, () => {
    state.auth.role = 'child';
    syncParent();
    parentMessage('家长授权已过期，请重新输入家长验证码。', true);
  });
  $('logout-panel').hidden = !authenticated;
  $('session-minutes').value = state.config.session_minutes;
  $('daily-minutes').value = state.config.daily_minutes;
  const silenceSelect = $('vad-silence');
  const silenceValue = String(state.config.vad_silence_ms);
  if (![...silenceSelect.options].some(option => option.value === silenceValue)) {
    const option = document.createElement('option');
    option.value = silenceValue; option.textContent = `自定停顿 · ${state.config.vad_silence_ms / 1000} 秒`;
    silenceSelect.append(option);
  }
  silenceSelect.value = silenceValue;
  $('allowed-start').value = state.config.allowed_start_hour;
  $('allowed-end').value = state.config.allowed_end_hour;
  $('memory-enabled').checked = !!state.memory.enabled;
  const preferenceContainer = $('memory-preferences');
  preferenceContainer.replaceChildren();
  for (const preference of state.preferenceOptions) {
    const label = document.createElement('label');
    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.value = preference;
    checkbox.checked = (state.memory.preferences || []).includes(preference);
    const text = document.createTextNode(preference);
    label.append(checkbox, text);
    preferenceContainer.append(label);
  }
  $('model-preset').options[0].textContent = state.provider === 'hybrid' ? '语音和审核在本机 · 文字由云端提供' : '本机运行 · 本地小模型';
  $('cloud-note').hidden = state.provider !== 'hybrid';
  updateTime();
}
function applyMe(data) {
  state.auth = { authenticated: !!data.authenticated, role: data.role || 'child' };
  if (data.provider) state.provider = data.provider;
  if (data.config) state.config = { ...state.config, ...data.config };
  if (data.memory) state.memory = { ...state.memory, ...data.memory };
  if (Array.isArray(data.preference_options)) state.preferenceOptions = data.preference_options;
  if (Number.isFinite(data.daily_used_seconds)) state.dailyUsed = data.daily_used_seconds;
  syncParent();
}
async function loadMe() {
  try { applyMe(await protocol.me()); }
  catch { notice('暂时连接不上小芽。请让大人检查这台电脑的服务是否开启。', true); }
}
async function checkHealth() {
  try {
    const health = await protocol.request(state.auth.role === 'parent' ? '/admin/status' : '/health');
    $('service-status').textContent = health.models ? health.models.ok ? '语音服务已准备好' : '语音服务准备中' : '语音服务已连接';
    $('model-status').textContent = `${state.provider === 'hybrid' ? '语音和审核本地 · 文字云端' : '本机运行'} · 今天已使用 ${Math.ceil((health.daily_used_seconds ?? state.dailyUsed) / 60)} 分钟`;
  } catch { $('service-status').textContent = '语音服务暂未连接'; $('model-status').textContent = '请检查这台电脑是否已经启动服务'; }
}
function openParent() {
  // Opening parent settings is an intentional break in the child's session.
  if (state.active || state.starting) stopSession('已暂停，请由大人完成设置后重新开始。', 'idle');
  parentMessage();
  syncParent();
  $('parent-dialog').showModal();
  void checkHealth();
}
async function closeParent() {
  window.babybotVoiceSettings?.stop();
  if (state.auth.role === 'parent') {
    try { applyMe(await protocol.lock()); }
    catch { parentMessage('设置还没有锁定成功，请稍后再试。', true); return; }
  }
  $('parent-dialog').close();
}
async function runForm(form, action) {
  const button = form.querySelector('[type="submit"]');
  if (button?.disabled) return;
  if (button) button.disabled = true;
  parentMessage();
  try { await action(); }
  catch (error) {
    if (error.status === 401 || error.status === 403) parentMessage('口令或验证码没有通过，请大人检查后再试。', true);
    else if (error.status === 429) parentMessage('尝试次数较多，请稍后再试。', true);
    else parentMessage('设置没有保存，请检查填写内容或稍后再试。', true);
  } finally { if (button) button.disabled = false; }
}

$('start-button').addEventListener('click', () => void startSession());
$('stop-button').addEventListener('click', () => stopSession('电话已挂断，麦克风已关闭。想玩的时候，再和大人一起回来吧。'));
$('mute-button').addEventListener('click', toggleMute);
$('continuous-mode').addEventListener('click', () => setVoiceMode('continuous'));
$('ptt-mode').addEventListener('click', () => setVoiceMode('ptt'));
$('parent-open').addEventListener('click', openParent);
$('parent-close').addEventListener('click', () => void closeParent());
$('parent-dialog').addEventListener('cancel', (event) => { event.preventDefault(); void closeParent(); });
$('parent-dialog').addEventListener('click', (event) => { if (event.target === $('parent-dialog') && event.clientX < $('parent-dialog').getBoundingClientRect().left) void closeParent(); });
state.selectedActivities.forEach((button) => button.addEventListener('click', () => {
  if (state.active || state.starting) return;
  state.mode = button.dataset.mode;
  state.selectedActivities.forEach((item) => { const selected = item === button; item.classList.toggle('active', selected); item.setAttribute('aria-pressed', String(selected)); });
}));
$('ptt-button').addEventListener('pointerdown', startPTT);
$('ptt-button').addEventListener('pointerup', () => releasePTT());
$('ptt-button').addEventListener('pointercancel', () => { releasePTT(false); resetVAD(); });
$('ptt-button').addEventListener('lostpointercapture', () => releasePTT());
$('ptt-button').addEventListener('contextmenu', (event) => event.preventDefault());
$('ptt-button').addEventListener('keydown', (event) => { if ([' ', 'Enter'].includes(event.key)) { event.preventDefault(); if (!event.repeat) startPTT(); } });
$('ptt-button').addEventListener('keyup', (event) => { if ([' ', 'Enter'].includes(event.key)) { event.preventDefault(); releasePTT(); } });
$('ptt-button').addEventListener('blur', () => releasePTT());
$('text-form').addEventListener('submit', (event) => {
  event.preventDefault();
  const text = $('text-input').value.trim();
  if (!text || !state.active) return;
  if (state.playback || state.queue.length || ['thinking', 'speaking', 'checking', 'transcribing'].includes(state.uiState)) interrupt();
  resetVAD();
  if (send({ type: 'text', text })) { $('text-input').value = ''; setStatus('thinking'); }
});
$('login-form').addEventListener('submit', (event) => {
  event.preventDefault();
  void runForm(event.currentTarget, async () => {
    applyMe(await protocol.login($('family-code').value));
    $('family-code').value = '';
    parentMessage('已经连接，可以开始说话了。');
    notice('已连接这个家庭。建议先由大人试用，再陪同孩子一起玩。');
    $('parent-dialog').close();
  });
});
$('parent-unlock-form').addEventListener('submit', (event) => {
  event.preventDefault();
  void runForm(event.currentTarget, async () => {
    const result = await protocol.parent($('parent-pin').value);
    $('parent-pin').value = '';
    if (result.authenticated !== undefined) applyMe(result);
    else await loadMe();
    const [settings, memory] = await Promise.all([protocol.settings(), protocol.memory()]);
    state.config = { ...state.config, ...(settings.config || settings) };
    state.memory = { ...state.memory, ...memory };
    syncParent();
    parentMessage('家长设置已解锁。');
    void checkHealth();
  });
});
$('settings-form').addEventListener('submit', (event) => {
  event.preventDefault();
  void runForm(event.currentTarget, async () => {
    const settings = { session_minutes: Number($('session-minutes').value), daily_minutes: Number($('daily-minutes').value), vad_silence_ms: Number($('vad-silence').value), allowed_start_hour: Number($('allowed-start').value), allowed_end_hour: Number($('allowed-end').value) };
    if (settings.allowed_start_hour >= settings.allowed_end_hour) { parentMessage('结束时刻需要晚于开始时刻。', true); return; }
    const result = await protocol.saveSettings(settings);
    state.config = { ...state.config, ...settings, ...(result.config || result) };
    syncParent();
    parentMessage('时间设置已保存，下次开始时生效。');
  });
});
$('memory-form').addEventListener('submit', (event) => {
  event.preventDefault();
  void runForm(event.currentTarget, async () => {
    const preferences = [...$('memory-preferences').querySelectorAll('input:checked')].map((input) => input.value);
    if (preferences.length > 5) { parentMessage('最多选择 5 个话题。', true); return; }
    const memory = { enabled: $('memory-enabled').checked, preferences };
    const result = await protocol.saveMemory(memory);
    state.memory = { ...memory, ...result };
    syncParent();
    parentMessage('偏好已保存。');
  });
});
$('delete-memory').addEventListener('click', async () => {
  try { await protocol.deleteMemory(); state.memory = { enabled: false, preferences: [] }; syncParent(); parentMessage('已清除所有话题偏好。'); }
  catch { parentMessage('还没有清除成功，请稍后再试。', true); }
});
$('pause-button').addEventListener('click', async () => {
  try { await protocol.pause(); parentMessage('所有设备都已停止对话。'); }
  catch { parentMessage('暂时没有完成，请稍后再试。', true); }
});
$('revoke-button').addEventListener('click', async () => {
  try { await protocol.revoke(); parentMessage('其他设备的会话已结束。'); }
  catch { parentMessage('暂时没有完成，请稍后再试。', true); }
});
$('logout-button').addEventListener('click', async () => {
  window.babybotVoiceSettings?.stop();
  stopSession('', 'idle');
  try { await protocol.logout(); applyMe({ authenticated: false }); parentMessage('这台设备已退出。'); }
  catch { parentMessage('退出没有完成，请稍后再试。', true); }
});
document.addEventListener('visibilitychange', () => { if (document.hidden && (state.active || state.starting)) stopSession('页面离开前台，麦克风已关闭。回到这里后请重新开始。', 'idle'); });
window.addEventListener('pagehide', () => stopSession('', 'idle'));
window.addEventListener('offline', () => { if (state.active || state.starting) stopSession('网络连接断开，麦克风已关闭。连接恢复后请重新开始。', 'idle'); });
updateControls();
void loadMe();
