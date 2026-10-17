#!/usr/bin/env node
/* Browser integration QA. Audio input uses an explicitly labelled virtual microphone. */
const { chromium } = require('/home/timocy/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const reports = path.join(root, 'reports');
const credentials = JSON.parse(fs.readFileSync(path.join(root, '.runtime/credentials.json')));
const base = process.env.BABYBOT_URL || 'http://127.0.0.1:8765';
const voicePath = process.argv.includes('--voice') ? process.argv[process.argv.indexOf('--voice') + 1] : null;
const inference = process.argv.includes('--inference');
const voiceOnly = process.argv.includes('--voice-only');
const allowSafeFallback = process.argv.includes('--allow-safe-fallback');
const customOutput = process.argv.includes('--output') ? process.argv[process.argv.indexOf('--output') + 1] : null;
if (voiceOnly && !voicePath) throw new Error('--voice-only requires --voice');
const report = { timestamp: new Date().toISOString(), base, browser: 'Chromium headless', microphone: voicePath ? 'File-backed virtual microphone; synthetic adult Mandarin speech' : 'Silent virtual microphone', tests: [], console_errors: [], page_errors: [], csp_violations: [], limitations: ['No physical Android device, real child voice, room acoustics or speaker echo cancellation was tested.'] };
fs.mkdirSync(path.join(reports, 'screenshots'), { recursive: true });
function silentWav(destination) {
  const seconds = 10;
  const bytes = 16000 * seconds * 2;
  const wav = Buffer.alloc(44 + bytes);
  wav.write('RIFF'); wav.writeUInt32LE(36 + bytes, 4); wav.write('WAVEfmt ', 8); wav.writeUInt32LE(16, 16);
  wav.writeUInt16LE(1, 20); wav.writeUInt16LE(1, 22); wav.writeUInt32LE(16000, 24); wav.writeUInt32LE(32000, 28);
  wav.writeUInt16LE(2, 32); wav.writeUInt16LE(16, 34); wav.write('data', 36); wav.writeUInt32LE(bytes, 40);
  fs.writeFileSync(destination, wav);
}
const silence = path.join(root, '.runtime/browser-silence.wav');
silentWav(silence);
let microphoneFile = silence;
if (voicePath) {
  const source = fs.readFileSync(voicePath);
  assert.equal(source.toString('ascii', 0, 4), 'RIFF');
  let pcm;
  for (let offset = 12; offset + 8 <= source.length;) {
    const size = source.readUInt32LE(offset + 4);
    const name = source.toString('ascii', offset, offset + 4);
    if (name === 'fmt ') {
      assert.equal(source.readUInt16LE(offset + 8), 1, 'fixture must be PCM');
      assert.equal(source.readUInt16LE(offset + 10), 1, 'fixture must be mono');
      assert.equal(source.readUInt32LE(offset + 12), 16000, 'fixture must be 16kHz');
      assert.equal(source.readUInt16LE(offset + 22), 16, 'fixture must be PCM16');
    }
    if (name === 'data') pcm = source.subarray(offset + 8, offset + 8 + size);
    offset += 8 + size + (size % 2);
  }
  assert.ok(pcm?.length);
  // Chrome loops fake audio. A long quiet tail prevents the fixture from
  // interrupting its own reply while preserving native capture and VAD.
  const payload = Buffer.concat([Buffer.alloc(16000), pcm, Buffer.alloc(60 * 32000)]);
  const wav = Buffer.concat([fs.readFileSync(silence).subarray(0, 44), payload]);
  wav.writeUInt32LE(wav.length - 8, 4); wav.writeUInt32LE(payload.length, 40);
  microphoneFile = path.join(root, '.runtime/browser-voice-input.wav');
  fs.writeFileSync(microphoneFile, wav);
}
async function check(name, action) {
  const started = Date.now();
  try { const details = await action(); report.tests.push({ name, passed: true, elapsed_ms: Date.now() - started, ...(details || {}) }); }
  catch (error) { report.tests.push({ name, passed: false, elapsed_ms: Date.now() - started, error: error.message }); throw error; }
}
(async () => {
  let browser;
  try {
    browser = await chromium.launch({ executablePath: '/opt/google/chrome/chrome', headless: true,
      args: ['--no-sandbox', '--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream', `--use-file-for-fake-audio-capture=${microphoneFile}`, '--autoplay-policy=no-user-gesture-required'] });
    const context = await browser.newContext({ viewport: { width: 1365, height: 1000 }, permissions: ['microphone'], locale: 'zh-CN' });
    const page = await context.newPage();
    page.on('console', (message) => { if (message.type() === 'error') report.console_errors.push(message.text()); });
    page.on('pageerror', (error) => report.page_errors.push(error.message));
    await page.addInitScript(() => {
      window.__capturedStreams = [];
      window.__sentEvents = [];
      window.__receivedEvents = [];
      window.__cspViolations = [];
      window.__playbackStarts = [];
      window.__stopClicks = [];
      window.__trackStops = [];
      document.addEventListener('click', event => { if (event.target.closest?.('#stop-button')) window.__stopClicks.push(performance.now()); }, true);
      const nativeAudioStart = AudioBufferSourceNode.prototype.start;
      AudioBufferSourceNode.prototype.start = function(...args) {
        const audio = [...window.__receivedEvents].reverse().find(event => event.type === 'audio' || event.type === 'greeting');
        window.__playbackStarts.push({ time: performance.now(), turn_id: audio?.turn_id, type: audio?.type, source: audio?.source, context_base_latency_s: this.context.baseLatency });
        return nativeAudioStart.apply(this, args);
      };
      document.addEventListener('securitypolicyviolation', (event) => window.__cspViolations.push({ directive: event.violatedDirective, blocked: event.blockedURI }));
      const capture = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
      navigator.mediaDevices.getUserMedia = async (...args) => {
        const stream = await capture(...args);
        for (const track of stream.getTracks()) {
          const stop = track.stop.bind(track);
          track.stop = () => { stop(); window.__trackStops.push({ time: performance.now(), state: track.readyState }); };
        }
        window.__capturedStreams.push(stream); return stream;
      };
      const NativeWS = window.WebSocket;
      window.WebSocket = class extends NativeWS {
        constructor(...args) { super(...args); this.addEventListener('message', (event) => { try { const m = JSON.parse(event.data); window.__receivedEvents.push({ type: m.type, turn_id: m.turn_id, source: m.source, category: m.category, text: m.text, safety: m.safety, time: performance.now() }); } catch {} }); }
        send(data) { window.__sentEvents.push(typeof data === 'string' ? { ...JSON.parse(data), time: performance.now() } : { type: 'pcm', bytes: data.byteLength, time: performance.now() }); return super.send(data); }
      };
    });
    await page.goto(base, { waitUntil: 'networkidle' });
    if (!voiceOnly) {
    await check('Desktop landing page renders without horizontal overflow', async () => {
      assert.equal(await page.title(), '小芽 · 一起听，一起想');
      await page.locator('#page-title').waitFor();
      assert.equal((await page.locator('#page-title').textContent()).replace(/\s/g, ''), '你好呀，今天想玩什么？');
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      assert.equal(await page.locator('[data-mode]').count(), 4);
      await page.screenshot({ path: path.join(reports, 'screenshots/desktop.png'), fullPage: true });
    });
    await check('390px mobile view has no horizontal overflow and retains usable controls', async () => {
      await page.setViewportSize({ width: 390, height: 844 });
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      const begin = await page.locator('#start-button').boundingBox();
      const stop = await page.locator('#stop-button').boundingBox();
      assert.ok(begin.height >= 44 && stop.height >= 44);
      await page.screenshot({ path: path.join(reports, 'screenshots/mobile.png'), fullPage: true });
      await page.setViewportSize({ width: 1365, height: 1000 });
    });
    await check('Family login, parent unlock, fixed preferences, settings save and relock', async () => {
      await page.locator('#parent-open').click();
      await page.locator('#family-code').fill(credentials.family_code);
      await page.locator('#login-form button').click();
      await page.locator('#parent-dialog').waitFor({ state: 'hidden' });
      await page.locator('#parent-open').click();
      await page.locator('#parent-pin').fill(credentials.parent_pin);
      await page.locator('#parent-unlock-form button').click();
      await page.locator('#settings-panel').waitFor({ state: 'visible' });
      assert.equal(await page.locator('#memory-preferences input').count(), 10);
      const before = await page.evaluate(() => fetch('/api/settings').then(r => r.json()));
      await page.locator('#settings-form button[type="submit"]').click();
      await page.getByText('时间设置已保存，下次开始时生效。', { exact: true }).waitFor();
      const after = await page.evaluate(() => fetch('/api/settings').then(r => r.json()));
      assert.deepEqual(after, before);
      await page.screenshot({ path: path.join(reports, 'screenshots/parent.png'), fullPage: true });
      await page.locator('#parent-close').click();
      await page.locator('#parent-dialog').waitFor({ state: 'hidden' });
      const me = await page.evaluate(() => fetch('/api/me').then(r => r.json()));
      assert.equal(me.role, 'child');
      report.provider = me.provider;
      if (inference || voicePath) assert.equal(me.provider, 'local', 'End-to-end verification requires local models');
    });
    } else {
      await check('Authenticate the browser against the local model service', async () => {
        const me = await page.evaluate(async code => {
          const response = await fetch('/api/auth/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ code }) });
          return response.json();
        }, credentials.family_code);
        assert.equal(me.authenticated, true);
        assert.equal(me.provider, 'local');
        report.provider = me.provider;
        await page.reload({ waitUntil: 'networkidle' });
      });
    }
    if (!voicePath) await check('Start obtains real browser microphone tracks and Stop ends every track', async () => {
      await page.locator('#start-button').click();
      await page.locator('#mic-indicator').filter({ hasText: '麦克风已开启' }).waitFor();
      assert.equal(await page.evaluate(() => window.__capturedStreams.at(-1).getAudioTracks()[0].readyState), 'live');
      await page.locator('#stop-button').click();
      assert.equal(await page.evaluate(() => window.__capturedStreams.every(s => s.getTracks().every(t => t.readyState === 'ended'))), true);
      assert.equal(await page.locator('#start-button').isEnabled(), true);
      assert.equal(await page.locator('#stop-button').isDisabled(), true);
      const timing = await page.evaluate(() => {
        const click = window.__stopClicks.at(-1);
        const stops = window.__trackStops.filter(item => item.time >= click);
        return { click_to_all_tracks_ended_ms: Math.max(...stops.map(item => item.time)) - click, all_states_ended: stops.every(item => item.state === 'ended') };
      });
      return { physical_microphone: false, native_getUserMedia: true, ...timing, timing_definition: 'DOM click capture to native MediaStreamTrack.stop return with readyState ended; excludes physical device shutdown latency' };
    });
    if (!voicePath) {
      await check('Push-to-talk responds to press/release and silence creates no model request', async () => {
        await page.locator('#start-button').click();
        await page.locator('#mic-indicator').filter({ hasText: '麦克风已开启' }).waitFor();
        await page.locator('#ptt-mode').click();
        const button = await page.locator('#ptt-button').boundingBox();
        const before = await page.evaluate(() => window.__sentEvents.filter(e => e.type === 'pcm').length);
        await page.mouse.move(button.x + button.width / 2, button.y + button.height / 2);
        await page.mouse.down();
        assert.equal(await page.locator('#ptt-button').textContent(), '正在听 · 松开发送');
        await page.waitForTimeout(500);
        await page.mouse.up();
        assert.equal(await page.locator('#ptt-button').textContent(), '按住说话');
        assert.equal(await page.evaluate(() => window.__sentEvents.filter(e => e.type === 'pcm').length), before);
        await page.locator('#stop-button').click();
        await page.locator('#continuous-mode').click();
      });
      await check('Actual page navigation fires pagehide and server session closes', async () => {
        await page.locator('#start-button').click();
        await page.locator('#mic-indicator').filter({ hasText: '麦克风已开启' }).waitFor();
        await page.evaluate(() => {
          const tracks = window.__capturedStreams.at(-1).getTracks();
          window.addEventListener('pagehide', () => sessionStorage.setItem('qa-tracks-ended', JSON.stringify(tracks.every(t => t.readyState === 'ended'))));
        });
        await page.goto(`${base}/?returned=1`, { waitUntil: 'networkidle' });
        assert.equal(await page.evaluate(() => sessionStorage.getItem('qa-tracks-ended')), 'true');
        assert.equal(await page.locator('#start-button').isEnabled(), true);
        assert.equal(await page.locator('#mic-indicator').textContent(), '麦克风已关闭');
        return { event: 'native pagehide caused by navigation' };
      });
      await check('Visibility safety handler ends tracks and requires a fresh start', async () => {
        await page.locator('#start-button').click();
        await page.locator('#mic-indicator').filter({ hasText: '麦克风已开启' }).waitFor();
        // Headless Chromium does not emulate physical lock/background behavior.
        // Test this handler explicitly, separately from native pagehide above.
        await page.evaluate(() => { Object.defineProperty(document, 'hidden', { configurable: true, get: () => true }); document.dispatchEvent(new Event('visibilitychange')); });
        assert.equal(await page.evaluate(() => window.__capturedStreams.every(s => s.getTracks().every(t => t.readyState === 'ended'))), true);
        assert.equal(await page.locator('#start-button').isEnabled(), true);
        await page.evaluate(() => { delete document.hidden; document.dispatchEvent(new Event('visibilitychange')); });
        assert.equal(await page.locator('#stop-button').isDisabled(), true);
        return { event: 'synthetic visibilitychange; native track disposal verified' };
      });
    }
    if (inference) {
      await check('Keyboard assist reaches the real local model and acknowledges actual audio completion', async () => {
        await page.locator('#start-button').click();
        await page.locator('#mic-indicator').filter({ hasText: '麦克风已开启' }).waitFor();
        await page.locator('#keyboard-details summary').click();
        await page.locator('#text-input').fill('小兔子在森林里发现一颗会发光的种子，我们接着编故事吧。');
        const started = Date.now();
        await page.locator('#send-button').click();
        await page.waitForFunction(() => window.__sentEvents.some(e => e.type === 'played' && e.chunk_id !== 'greeting'), null, { timeout: 120000 });
        const events = await page.evaluate(() => ({ sent: window.__sentEvents, received: window.__receivedEvents, playback: window.__playbackStarts }));
        const audio = events.received.find(e => e.type === 'audio');
        assert.ok(audio);
        assert.equal(audio.safety, 'approved');
        assert.equal(audio.source, 'generated');
        assert.ok(events.sent.find(e => e.type === 'played' && e.chunk_id !== 'greeting').time > audio.time);
        await page.screenshot({ path: path.join(reports, 'screenshots/conversation.png'), fullPage: true });
        await page.locator('#stop-button').click();
        const input = events.sent.filter(event => event.type === 'text').at(-1);
        const playback = events.playback.find(event => event.type === 'audio' && event.turn_id === audio.turn_id);
        return { elapsed_to_completed_playback_ms: Date.now() - started, text_send_to_audio_start_ms: playback.time - input.time, source: audio.source, reply: audio.text, timing_definition: 'WebSocket text send to AudioBufferSourceNode.start invocation; excludes speaker hardware latency' };
      });
    }
    if (voicePath) {
      await check(allowSafeFallback ? 'Final-policy microphone check: first utterance, safe response and completed playback' : 'First file-backed utterance interrupts greeting and traverses capture, VAD, real ASR, LLM, review and playback', async () => {
        const before = await page.evaluate(() => window.__sentEvents.filter(e => e.type === 'played' && e.chunk_id !== 'greeting').length);
        await page.locator('#start-button').click();
        await page.waitForFunction(() => window.__sentEvents.some(event => event.type === 'pcm'), null, { timeout: 12000 });
        await page.waitForFunction(count => window.__sentEvents.filter(e => e.type === 'played' && e.chunk_id !== 'greeting').length > count, before, { timeout: 120000 });
        const events = await page.evaluate(() => ({ sent: window.__sentEvents, received: window.__receivedEvents, playback: window.__playbackStarts }));
        const pcm = events.sent.filter(e => e.type === 'pcm').at(-1);
        const transcript = events.received.filter(e => e.type === 'transcript').at(-1);
        const audio = events.received.filter(e => e.type === 'audio').at(-1);
        assert.ok(pcm && pcm.bytes > 3200);
        const start = events.sent.filter(event => event.type === 'start').at(-1);
        assert.ok(pcm.time - start.time < 10000, 'first utterance must be captured, without waiting for fixture loop');
        assert.ok(events.sent.some(event => event.type === 'interrupt' && event.time < pcm.time), 'voice must interrupt the identity greeting');
        assert.ok(transcript?.text);
        assert.equal(audio.safety, 'approved');
        if (allowSafeFallback) assert.ok(audio.source === 'generated' || (audio.source === 'fallback' && audio.category === 'blocked'), 'Expected generated response or explicit safety rejection; unavailable fallback is not a pass');
        else assert.equal(audio.source, 'generated');
        await page.locator('#stop-button').click();
        const playback = events.playback.find(event => event.type === 'audio' && event.turn_id === audio.turn_id);
        assert.ok(playback);
        return { pcm_bytes: pcm.bytes, transcript: transcript.text, reply: audio.text, source: audio.source, category: audio.category || null, generated_response_delivered: audio.source === 'generated', event_timeline: { sent: events.sent.map(({type,time,bytes,chunk_id}) => ({type,time,bytes,chunk_id})), received: events.received.map(({type,time,turn_id,source,category}) => ({type,time,turn_id,source,category})), playback: events.playback }, pcm_send_to_audio_start_ms: playback.time - pcm.time, timing_definition: 'WebSocket binary PCM send to matching non-greeting AudioBufferSourceNode.start invocation; excludes VAD silence wait and speaker hardware latency', context_base_latency_s: playback.context_base_latency_s };
      });
    }
    report.csp_violations = await page.evaluate(() => window.__cspViolations);
    await check('No JavaScript, console or Content Security Policy errors', async () => {
      assert.deepEqual(report.page_errors, []);
      assert.deepEqual(report.console_errors, []);
      assert.deepEqual(report.csp_violations, []);
    });
    report.passed = true;
  } catch (error) { report.passed = false; report.failure = error.message; process.exitCode = 1; }
  finally {
    if (browser) await browser.close();
    const destination = customOutput ? path.resolve(root, customOutput) : path.join(reports, voicePath ? 'browser-voice.json' : inference ? 'browser-inference.json' : 'browser.json');
    if (fs.existsSync(destination)) {
      const history = path.join(reports, 'history'); fs.mkdirSync(history, { recursive: true });
      fs.copyFileSync(destination, path.join(history, `${path.basename(destination, '.json')}-${Date.now()}.json`));
    }
    fs.writeFileSync(destination, JSON.stringify(report, null, 2));
    console.log(JSON.stringify({ report: destination, passed: report.passed, tests: report.tests.map(t => ({ name: t.name, passed: t.passed })), failure: report.failure }, null, 2));
  }
})();
