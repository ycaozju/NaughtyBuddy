// Adult-only voice tools. Reference audio stays in memory until Create is clicked.
const voiceElement = (id) => document.getElementById(id);
const voiceState = {
  unlocked: false, catalog: null, busy: '', operation: 0, controller: null,
  recording: false, recorder: null, stream: null, chunks: [], since: 0, timer: null, stopTimer: null,
  sampleEpoch: 0, sample: null, sampleSeconds: 0, context: null, playback: null, onExpired: null,
};
const maxVoiceFileBytes = 10 * 1024 * 1024;

function voiceMessage(text = '', error = false) {
  voiceElement('voice-message').textContent = text;
  voiceElement('voice-message').classList.toggle('error', error);
}
function voiceLimits() {
  return { min_seconds: 3, max_seconds: 20, max_custom: 3, ...voiceState.catalog?.limits };
}
function chosenVoice() {
  return voiceState.catalog?.voices?.find((voice) => voice.id === voiceElement('voice-select').value);
}
function mayCreateVoice() {
  return voiceState.unlocked && voiceState.catalog?.clone_available &&
    voiceState.catalog.voices.filter((voice) => voice.kind === 'custom').length < voiceLimits().max_custom;
}
function renderVoiceControls() {
  const locked = !voiceState.unlocked;
  const busy = !!voiceState.busy || voiceState.recording;
  const selected = chosenVoice();
  const canCreate = mayCreateVoice();
  const consent = voiceElement('voice-consent').checked;
  voiceElement('voice-settings-panel').hidden = locked;
  voiceElement('voice-select').disabled = locked || busy || !voiceState.catalog;
  voiceElement('voice-preview').disabled = locked || busy || !selected?.available;
  voiceElement('voice-preview-stop').disabled = !voiceState.playback && voiceState.busy !== 'preview';
  voiceElement('voice-save').disabled = locked || busy || !selected?.available || selected.id === voiceState.catalog?.selected_id;
  voiceElement('voice-save').textContent = voiceState.busy === 'select' ? '正在准备音色…' : '保存音色';
  voiceElement('voice-delete').hidden = selected?.kind !== 'custom';
  voiceElement('voice-delete-hint').hidden = selected?.kind !== 'custom';
  voiceElement('voice-delete').disabled = locked || busy || selected?.kind !== 'custom';
  voiceElement('voice-description').textContent = selected ? `${selected.description || ''}${selected.id === voiceState.catalog?.selected_id ? ' · 当前使用' : ''}${selected.available ? '' : ' · 本机暂不可用'}` : '';
  voiceElement('voice-clone-availability').textContent = !voiceState.catalog ? '正在读取本机音色信息…' :
    !voiceState.catalog.clone_available ? '本机声音克隆模型尚未就绪，请先由大人完成模型安装。' :
      !canCreate ? `已达到 ${voiceLimits().max_custom} 个自定义音色，可先删除不再使用的音色。` : '参考录音与音色模型只在这台电脑处理。';
  for (const id of ['voice-name', 'voice-reference-text', 'voice-consent']) voiceElement(id).disabled = locked || busy || !canCreate;
  voiceElement('voice-record-start').disabled = locked || busy || !canCreate || !consent;
  voiceElement('voice-record-stop').disabled = !voiceState.recording || !voiceState.recorder;
  voiceElement('voice-record-cancel').disabled = !voiceState.recording && voiceState.busy !== 'convert';
  voiceElement('voice-file').disabled = locked || busy || !canCreate || !consent;
  voiceElement('voice-sample-preview').disabled = locked || busy || !voiceState.sample;
  voiceElement('voice-sample-clear').disabled = locked || busy || !voiceState.sample;
  voiceElement('voice-create').disabled = locked || busy || !canCreate || !consent || !voiceState.sample ||
    !voiceElement('voice-name').value.trim() || voiceElement('voice-name').value.trim().length > 24 ||
    voiceElement('voice-reference-text').value.trim().length < 5 || voiceElement('voice-reference-text').value.trim().length > 200;
  voiceElement('voice-create').textContent = voiceState.busy === 'create' ? '正在创建音色…' : '创建自定义音色';
  voiceElement('voice-record-status').classList.toggle('recording', voiceState.recording && !!voiceState.stream);
  voiceElement('voice-record-label').textContent = voiceState.recording ? voiceState.stream ? '正在录音' : '等待麦克风许可…' : '麦克风已关闭';
}
function applyVoiceCatalog(catalog, preferredId) {
  if (!catalog || !Array.isArray(catalog.voices)) throw new Error('音色列表没有读取完整，请重新打开家长空间。');
  voiceState.catalog = catalog;
  const select = voiceElement('voice-select');
  select.replaceChildren();
  for (const voice of catalog.voices) {
    const option = document.createElement('option');
    option.value = voice.id;
    option.textContent = `${voice.name}${voice.kind === 'custom' ? ' · 自定义' : ''}${voice.available ? '' : ' · 暂不可用'}`;
    select.append(option);
  }
  select.value = catalog.voices.some((voice) => voice.id === preferredId) ? preferredId : catalog.selected_id;
  renderVoiceControls();
}
async function voiceRequest(path, method = 'GET', body, signal, audio = false) {
  const response = await fetch(`/api/voices${path}`, {
    method, credentials: 'same-origin', cache: 'no-store', signal,
    headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    let value = {};
    try { value = await response.json(); } catch { /* Use the status-specific fallback. */ }
    const error = new Error(typeof value.detail === 'string' ? value.detail : value.message || (response.status === 422 ? '请检查音色名称（1–24字）、逐字稿（5–200字）和参考录音格式。' : '音色操作没有完成，请稍后再试。'));
    error.status = response.status;
    throw error;
  }
  if (audio) {
    if (!(response.headers.get('Content-Type') || '').toLowerCase().includes('audio/wav')) throw new Error('试听声音格式不正确，未播放。');
    return response.arrayBuffer();
  }
  return response.json();
}
function currentVoiceOperation(token) { return voiceState.unlocked && token === voiceState.operation; }
function reportVoiceError(error) {
  if (error.status === 401 || error.status === 403) {
    setVoiceUnlocked(false);
    voiceState.onExpired?.();
    return;
  }
  const message = error.name === 'AbortError' ? '等待时间较长，请稍后再试；已保存的音色可以重新读取。' :
    error.name === 'NotAllowedError' ? '麦克风许可未开启，请允许麦克风，或选择一段已有录音。' :
      error.name === 'NotFoundError' ? '没有找到麦克风，可以连接麦克风或选择已有录音。' :
        error.status === 503 ? `本机音色服务尚未就绪。${error.message}` : error.message || '音色操作没有完成，请稍后再试。';
  voiceMessage(message, true);
}
async function voiceOperation(kind, message, action) {
  if (!voiceState.unlocked || voiceState.busy || voiceState.recording) return;
  stopVoicePlayback();
  const token = ++voiceState.operation;
  const controller = new AbortController();
  voiceState.controller = controller;
  voiceState.busy = kind;
  voiceMessage(message);
  renderVoiceControls();
  const timeout = setTimeout(() => controller.abort(), 135000);
  try { await action(controller.signal, token); }
  catch (error) { if (currentVoiceOperation(token)) reportVoiceError(error); }
  finally {
    clearTimeout(timeout);
    if (token === voiceState.operation) {
      voiceState.controller = null;
      voiceState.busy = '';
      renderVoiceControls();
    }
  }
}
function getVoiceAudioContext() {
  if (!voiceState.context || voiceState.context.state === 'closed') {
    const AudioContext = window.AudioContext || window.webkitAudioContext;
    if (!AudioContext) throw new Error('这个浏览器还不能处理音频，请换用更新的浏览器。');
    voiceState.context = new AudioContext();
  }
  return voiceState.context;
}
function stopVoicePlayback() {
  const source = voiceState.playback;
  voiceState.playback = null;
  if (source) {
    source.onended = null;
    try { source.stop(); } catch { /* Already ended. */ }
    source.disconnect();
  }
}
async function playVoiceBytes(bytes, context, valid) {
  const audio = await context.decodeAudioData(bytes.slice(0));
  if (!valid() || context.state === 'closed') return;
  stopVoicePlayback();
  const source = context.createBufferSource();
  source.buffer = audio;
  source.connect(context.destination);
  voiceState.playback = source;
  source.onended = () => {
    source.disconnect();
    if (voiceState.playback === source) { voiceState.playback = null; renderVoiceControls(); }
  };
  source.start();
  renderVoiceControls();
}
function previewSelectedVoice() {
  if (!voiceState.unlocked || voiceState.busy || !chosenVoice()?.available) return;
  // Create/resume directly in the adult's click handler, before the HTTP request.
  let context, resume;
  try { context = getVoiceAudioContext(); resume = context.resume(); }
  catch (error) { reportVoiceError(error); return; }
  const id = chosenVoice().id;
  void voiceOperation('preview', '正在准备固定试听句，请稍等…', async (signal, token) => {
    await resume;
    const bytes = await voiceRequest('/preview', 'POST', { voice_id: id }, signal, true);
    await playVoiceBytes(bytes, context, () => currentVoiceOperation(token));
    if (currentVoiceOperation(token)) voiceMessage('正在试听 AI 合成的声音。');
  });
}

// Downmix and resample locally; only this PCM16, 16 kHz mono WAV reaches the API.
function encodeVoiceWav(audio) {
  const outputRate = 16000;
  const frameCount = Math.round(audio.duration * outputRate);
  const bytes = new ArrayBuffer(44 + frameCount * 2);
  const view = new DataView(bytes);
  const text = (offset, value) => { for (let i = 0; i < value.length; i++) view.setUint8(offset + i, value.charCodeAt(i)); };
  text(0, 'RIFF'); view.setUint32(4, bytes.byteLength - 8, true); text(8, 'WAVE'); text(12, 'fmt ');
  view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
  view.setUint32(24, outputRate, true); view.setUint32(28, outputRate * 2, true);
  view.setUint16(32, 2, true); view.setUint16(34, 16, true); text(36, 'data'); view.setUint32(40, frameCount * 2, true);
  const channels = Array.from({ length: audio.numberOfChannels }, (_, index) => audio.getChannelData(index));
  const ratio = audio.sampleRate / outputRate;
  for (let i = 0; i < frameCount; i++) {
    const start = Math.min(audio.length - 1, Math.floor(i * ratio));
    const end = Math.min(audio.length, Math.max(start + 1, Math.floor((i + 1) * ratio)));
    let sum = 0;
    for (let j = start; j < end; j++) for (const channel of channels) sum += channel[j];
    const value = Math.max(-1, Math.min(1, sum / ((end - start) * channels.length)));
    view.setInt16(44 + i * 2, Math.round(value * (value < 0 ? 32768 : 32767)), true);
  }
  return bytes;
}
async function prepareVoiceSample(bytes, epoch) {
  if (epoch !== voiceState.sampleEpoch || !voiceState.unlocked) return;
  if (bytes.byteLength > maxVoiceFileBytes) throw new Error('录音文件不能超过 10 MB，请选择更短的一段。');
  const context = getVoiceAudioContext();
  let audio;
  try { audio = await context.decodeAudioData(bytes.slice(0)); }
  catch { throw new Error('这个音频没有解码成功，请试试 WAV、MP3 或浏览器录制的音频。'); }
  if (epoch !== voiceState.sampleEpoch || !voiceState.unlocked) return;
  const limits = voiceLimits();
  if (!Number.isFinite(audio.duration) || audio.duration < limits.min_seconds || audio.duration > limits.max_seconds) {
    throw new Error(`参考录音需要 ${limits.min_seconds}–${limits.max_seconds} 秒；这段约 ${audio.duration.toFixed(1)} 秒，请重新录制或选择文件。`);
  }
  voiceState.sample = encodeVoiceWav(audio);
  voiceState.sampleSeconds = audio.duration;
  voiceElement('voice-sample-status').textContent = `参考录音已就绪 · ${audio.duration.toFixed(1)} 秒 · 还未提交`;
  voiceMessage('请核对逐字稿与录音一致，然后点击“创建自定义音色”。');
}
function stopVoiceRecording(discard = true) {
  clearInterval(voiceState.timer);
  clearTimeout(voiceState.stopTimer);
  voiceState.timer = voiceState.stopTimer = null;
  const recorder = voiceState.recorder;
  voiceState.recorder = null;
  voiceState.recording = false;
  if (recorder) {
    if (discard) recorder.ondataavailable = recorder.onstop = recorder.onerror = null;
    if (recorder.state !== 'inactive') recorder.stop();
  }
  voiceState.stream?.getTracks().forEach((track) => track.stop());
  voiceState.stream = null;
  if (discard) voiceState.chunks.length = 0;
}
function clearVoiceSample() {
  voiceState.sampleEpoch++;
  stopVoiceRecording();
  stopVoicePlayback();
  voiceState.sample = null;
  voiceState.sampleSeconds = 0;
  voiceElement('voice-file').value = '';
  voiceElement('voice-record-time').textContent = '00:00';
  voiceElement('voice-sample-status').textContent = '还没有参考录音。';
  if (voiceState.busy === 'convert') voiceState.busy = '';
  renderVoiceControls();
}
function finishVoiceRecording() {
  if (!voiceState.recording || !voiceState.recorder) return;
  voiceState.busy = 'convert';
  stopVoiceRecording(false);
  voiceMessage('麦克风已关闭，正在整理参考录音…');
  renderVoiceControls();
}
async function startVoiceRecording() {
  if (!mayCreateVoice() || !voiceElement('voice-consent').checked || voiceState.busy || voiceState.recording) return;
  clearVoiceSample();
  const epoch = voiceState.sampleEpoch;
  voiceState.recording = true;
  voiceMessage('请朗读与逐字稿相同的内容，完成后点击停止录音。');
  renderVoiceControls();
  try {
    if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) throw new Error('这个浏览器还不能录音，请选择已有音频文件。');
    const stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true }, video: false });
    if (epoch !== voiceState.sampleEpoch || !voiceState.unlocked || document.hidden) { stream.getTracks().forEach((track) => track.stop()); return; }
    voiceState.stream = stream;
    const MediaRecorder = window.MediaRecorder;
    const mimeType = ['audio/webm;codecs=opus', 'audio/mp4', 'audio/ogg;codecs=opus'].find((type) => MediaRecorder.isTypeSupported?.(type));
    const recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
    voiceState.recorder = recorder;
    const chunks = voiceState.chunks = [];
    recorder.ondataavailable = (event) => { if (epoch === voiceState.sampleEpoch && event.data.size) chunks.push(event.data); };
    recorder.onstop = async () => {
      if (epoch !== voiceState.sampleEpoch || !voiceState.unlocked) return;
      stopVoiceRecording(false);
      voiceState.busy = 'convert';
      renderVoiceControls();
      try { await prepareVoiceSample(await new Blob(chunks, { type: recorder.mimeType }).arrayBuffer(), epoch); }
      catch (error) { if (epoch === voiceState.sampleEpoch && voiceState.unlocked) reportVoiceError(error); }
      finally {
        chunks.length = 0;
        if (epoch === voiceState.sampleEpoch) { voiceState.busy = ''; renderVoiceControls(); }
      }
    };
    recorder.onerror = () => {
      if (epoch !== voiceState.sampleEpoch) return;
      clearVoiceSample();
      voiceMessage('录音没有完成，麦克风已关闭。可以重新录制或选择文件。', true);
    };
    recorder.start(250);
    voiceState.since = Date.now();
    voiceState.timer = setInterval(() => {
      const seconds = Math.floor((Date.now() - voiceState.since) / 1000);
      voiceElement('voice-record-time').textContent = `00:${String(seconds).padStart(2, '0')}`;
    }, 100);
    // Allow a little room for MediaRecorder's final container/audio frame.
    voiceState.stopTimer = setTimeout(finishVoiceRecording, voiceLimits().max_seconds * 1000 - 250);
    renderVoiceControls();
  } catch (error) {
    if (epoch !== voiceState.sampleEpoch) return;
    clearVoiceSample();
    reportVoiceError(error);
  }
}
async function chooseVoiceFile() {
  const file = voiceElement('voice-file').files?.[0];
  if (!file || !mayCreateVoice() || !voiceElement('voice-consent').checked || voiceState.busy || voiceState.recording) return;
  clearVoiceSample();
  const epoch = voiceState.sampleEpoch;
  voiceState.busy = 'convert';
  voiceMessage('正在本地检查这段录音…');
  renderVoiceControls();
  try {
    if (file.size > maxVoiceFileBytes) throw new Error('录音文件不能超过 10 MB，请选择更短的一段。');
    await prepareVoiceSample(await file.arrayBuffer(), epoch);
  } catch (error) { if (epoch === voiceState.sampleEpoch && voiceState.unlocked) reportVoiceError(error); }
  finally { if (epoch === voiceState.sampleEpoch) { voiceState.busy = ''; renderVoiceControls(); } }
}
function voiceBase64(bytes) {
  const data = new Uint8Array(bytes);
  let value = '';
  for (let i = 0; i < data.length; i += 8192) value += String.fromCharCode(...data.subarray(i, i + 8192));
  return btoa(value);
}
function stopVoiceSettings() {
  voiceState.operation++;
  voiceState.controller?.abort();
  voiceState.controller = null;
  voiceState.busy = '';
  clearVoiceSample();
  if (voiceState.context) {
    const context = voiceState.context;
    voiceState.context = null;
    if (context.state !== 'closed') void context.close().catch(() => {});
  }
  voiceElement('voice-name').value = '';
  voiceElement('voice-reference-text').value = '';
  voiceElement('voice-consent').checked = false;
  voiceMessage();
  renderVoiceControls();
}
function setVoiceUnlocked(unlocked, onExpired) {
  if (onExpired) voiceState.onExpired = onExpired;
  const changed = !!unlocked !== voiceState.unlocked;
  voiceState.unlocked = !!unlocked;
  if (!unlocked) { stopVoiceSettings(); voiceState.catalog = null; }
  renderVoiceControls();
  if (unlocked && changed) void voiceOperation('catalog', '正在读取本机音色…', async (signal, token) => {
    const catalog = await voiceRequest('', 'GET', undefined, signal);
    if (currentVoiceOperation(token)) { applyVoiceCatalog(catalog); voiceMessage(); }
  });
}

voiceElement('voice-select').addEventListener('change', () => { stopVoicePlayback(); renderVoiceControls(); });
voiceElement('voice-preview').addEventListener('click', previewSelectedVoice);
voiceElement('voice-preview-stop').addEventListener('click', () => {
  if (voiceState.busy === 'preview') { voiceState.operation++; voiceState.controller?.abort(); voiceState.controller = null; voiceState.busy = ''; }
  stopVoicePlayback(); voiceMessage('试听已停止。'); renderVoiceControls();
});
voiceElement('voice-select-form').addEventListener('submit', (event) => {
  event.preventDefault();
  const selected = chosenVoice();
  if (!selected?.available) return;
  void voiceOperation('select', '正在准备这个音色，请保持页面开启；完成后下次拨打生效。', async (signal, token) => {
    const catalog = await voiceRequest('/select', 'POST', { voice_id: selected.id }, signal);
    if (currentVoiceOperation(token)) { applyVoiceCatalog(catalog); voiceMessage('音色已保存，下次拨打时使用。'); }
  });
});
voiceElement('voice-delete').addEventListener('click', () => {
  const selected = chosenVoice();
  if (selected?.kind !== 'custom') return;
  void voiceOperation('delete', '正在删除参考录音和音色缓存…', async (signal, token) => {
    const catalog = await voiceRequest(`/${encodeURIComponent(selected.id)}`, 'DELETE', undefined, signal);
    if (currentVoiceOperation(token)) { applyVoiceCatalog(catalog); voiceMessage('自定义音色及其参考录音已删除。'); }
  });
});
voiceElement('voice-record-start').addEventListener('click', () => void startVoiceRecording());
voiceElement('voice-record-stop').addEventListener('click', finishVoiceRecording);
voiceElement('voice-record-cancel').addEventListener('click', () => { clearVoiceSample(); voiceMessage('已取消，麦克风已关闭。'); });
voiceElement('voice-file').addEventListener('change', () => void chooseVoiceFile());
voiceElement('voice-sample-clear').addEventListener('click', () => { clearVoiceSample(); voiceMessage('尚未提交的参考录音已清除。'); });
voiceElement('voice-sample-preview').addEventListener('click', () => {
  if (!voiceState.unlocked || !voiceState.sample || voiceState.busy || voiceState.recording) return;
  const bytes = voiceState.sample;
  let context, resume;
  try { context = getVoiceAudioContext(); resume = context.resume(); }
  catch (error) { reportVoiceError(error); return; }
  void voiceOperation('preview', '准备播放这段参考录音…', async (_signal, token) => {
    await resume;
    await playVoiceBytes(bytes, context, () => currentVoiceOperation(token));
    if (currentVoiceOperation(token)) voiceMessage('正在试听尚未提交的参考录音。');
  });
});
for (const id of ['voice-name', 'voice-reference-text', 'voice-consent']) voiceElement(id).addEventListener('input', renderVoiceControls);
voiceElement('voice-clone-form').addEventListener('submit', (event) => {
  event.preventDefault();
  const name = voiceElement('voice-name').value.trim();
  const referenceText = voiceElement('voice-reference-text').value.trim();
  if (!mayCreateVoice() || !voiceState.sample || !name || name.length > 24 || referenceText.length < 5 || referenceText.length > 200 || !voiceElement('voice-consent').checked) return;
  const body = { name, reference_text: referenceText, audio_base64: voiceBase64(voiceState.sample), consent: true };
  void voiceOperation('create', '正在保存参考录音并创建本机音色…', async (signal, token) => {
    const before = new Set(voiceState.catalog.voices.map((voice) => voice.id));
    const catalog = await voiceRequest('', 'POST', body, signal);
    if (currentVoiceOperation(token)) {
      const created = catalog.voices.find((voice) => !before.has(voice.id));
      clearVoiceSample();
      voiceElement('voice-name').value = '';
      voiceElement('voice-reference-text').value = '';
      voiceElement('voice-consent').checked = false;
      applyVoiceCatalog(catalog, catalog.created_id || created?.id);
      voiceMessage('自定义音色已创建。可以先试听，再点击“保存音色”用于下次通话。');
    }
  });
});
voiceElement('parent-dialog').addEventListener('close', stopVoiceSettings);
document.addEventListener('visibilitychange', () => { if (document.hidden) stopVoiceSettings(); });
window.addEventListener('pagehide', stopVoiceSettings);
window.addEventListener('offline', stopVoiceSettings);
window.babybotVoiceSettings = { setUnlocked: setVoiceUnlocked, stop: stopVoiceSettings };
setVoiceUnlocked(!!window.babybotParentUnlocked);
