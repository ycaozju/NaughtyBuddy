const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../../static/app.js'), 'utf8');

function app() {
  const elements = new Map();
  const documentListeners = new Map();
  const windowListeners = new Map();
  function element() {
    const children = [];
    const attributes = new Map();
    const classes = new Set();
    const listeners = new Map();
    let span;
    return {
      textContent: '', value: '', hidden: false, disabled: false, dataset: {}, children,
      lastChild: { textContent: '' }, options: [{ textContent: '' }],
      classList: { add: value => classes.add(value), remove: value => classes.delete(value), contains: value => classes.has(value), toggle: (value, on) => on ? classes.add(value) : classes.delete(value) },
      addEventListener: (name, fn) => listeners.set(name, fn), listeners,
      querySelector: selector => selector === '.empty-conversation' ? null : selector === 'span' ? span ||= element() : element(), querySelectorAll: () => [],
      append: (...values) => children.push(...values), replaceChildren: () => { children.length = 0; },
      setAttribute: (key, value) => attributes.set(key, value), getAttribute: key => attributes.get(key), showModal() {}, close() {},
    };
  }
  const context = vm.createContext({
    document: { hidden: false, getElementById: (id) => { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); }, querySelectorAll: () => [], addEventListener: (name, fn) => documentListeners.set(name, fn), createElement: element, createTextNode: (text) => ({ textContent: text }) },
    window: { addEventListener: (name, fn) => windowListeners.set(name, fn) }, location: { protocol: 'http:', host: 'localhost:8765' },
    fetch: async () => ({ ok: true, json: async () => ({ authenticated: false }) }),
    WebSocket: { OPEN: 1, CLOSING: 2 }, setInterval: () => 1, clearInterval() {},
    setTimeout: () => 2, clearTimeout() {}, Date, atob, navigator: {},
  });
  vm.runInContext(source, context);
  return { context, run: (code) => vm.runInContext(code, context), elements, documentListeners, windowListeners };
}

test('interruption stops local audio before sending and retires late audio', () => {
  const { run } = app();
  run(`globalThis.events = []; state.active = true; state.currentTurn = 4;
    state.socket = { readyState: 1, send: x => events.push(JSON.parse(x).type) };
    state.playback = { source: { stop: () => events.push('local-stop'), disconnect() {} } };
    interrupt();`);
  assert.deepEqual(Array.from(run('events')), ['local-stop', 'interrupt']);
  assert.equal(run('state.ignoredTurns.has("4")'), true);
  run(`handleMessage({ type: 'audio', turn_id: 4, chunk_id: 'late', safety: 'approved', mime: 'audio/wav', audio_base64: '' });`);
  assert.equal(run('state.queue.length'), 0);
  assert.equal(run('state.playback'), null);
});

test('a delayed interruption acknowledgement cannot cancel a newer turn', () => {
  const { run } = app();
  run(`globalThis.stopped = false; state.active = true; state.currentTurn = 8;
    state.playback = { source: { stop: () => stopped = true, disconnect() {} } };
    handleMessage({ type: 'interrupted', turn_id: 7, canceled_turn_id: 6 });`);
  assert.equal(run('stopped'), false);
  assert.equal(run('state.ignoredTurns.has("6")'), true);
});

test('unapproved audio is never decoded or queued', () => {
  const { run } = app();
  run(`state.active = true; handleMessage({ type: 'audio', turn_id: 2, safety: 'rejected', mime: 'audio/wav', audio_base64: 'not audio' });`);
  assert.equal(run('state.queue.length'), 0);
  assert.equal(run('state.playback'), null);
});

test('ending closes microphone, audio graph, timers and transport', () => {
  const { run, elements } = app();
  run(`globalThis.events = []; state.active = true;
    state.socket = { readyState: 1, send: x => events.push(JSON.parse(x).type), close: () => events.push('close-socket') };
    state.stream = { getTracks: () => [{ stop: () => events.push('stop-microphone') }] };
    state.recorder = { port: {}, disconnect: () => events.push('disconnect-recorder') };
    state.source = { disconnect: () => events.push('disconnect-source') };
    state.context = { state: 'running', close: () => { events.push('close-context'); return Promise.resolve(); } };
    stopSession();`);
  assert.equal(run('state.active'), false);
  assert.equal(run('state.stream'), null);
  assert.equal(run('state.socket'), null);
  assert.equal(run('state.context'), null);
  assert.equal(run('state.clock'), null);
  assert.deepEqual(Array.from(run('events')), ['stop', 'disconnect-recorder', 'disconnect-source', 'stop-microphone', 'close-context', 'close-socket']);
  assert.equal(elements.get('stop-button').disabled, true);
  assert.equal(elements.get('start-button').disabled, false);
});

test('VAD includes preroll and waits through a thinking pause before sending one turn', () => {
  const { run } = app();
  run(`globalThis.sent = []; state.active = true; state.uiState = 'listening';
    state.socket = { readyState: 1, send: x => sent.push(x) };
    globalThis.frame = rms => captureFrame({ pcm: new Int16Array(320).fill(Math.round(rms * 32767)).buffer, rms });
    for(let i = 0; i < 20; i++) frame(.001);
    for(let i = 0; i < 30; i++) frame(.1);
    for(let i = 0; i < 50; i++) frame(.001);`);
  assert.equal(run('sent.length'), 0, 'one-second thinking pause must not cut off speech');
  run('for(let i = 0; i < 20; i++) frame(.001);');
  assert.equal(run('sent.length'), 1);
  assert.ok(run('sent[0].byteLength') > (30 + 70) * 640, 'preroll precedes detected speech');
  assert.equal(run('state.speech'), false);
  assert.equal(run('state.uiState'), 'transcribing');
});

test('two failed recognitions keep the call listening and only suggest optional push-to-talk', () => {
  const { run, elements } = app();
  run(`globalThis.sent = []; state.active = true;
    state.socket = { readyState: 1, send: x => sent.push(x) };
    handleMessage({type:'error', code:'asr_unclear', retryable:true});
    handleMessage({type:'error', code:'asr_unclear', retryable:true});`);
  assert.equal(run('state.voiceMode'), 'continuous');
  assert.equal(elements.get('ptt-row').hidden, true);
  assert.equal(elements.get('ptt-mode').classList.contains('recommended'), true);
  run(`for(let i=0; i<30; i++) captureFrame({pcm:new Int16Array(320).fill(16000).buffer,rms:.5});
    for(let i=0; i<70; i++) captureFrame({pcm:new Int16Array(320).buffer,rms:0});`);
  assert.equal(run('sent.filter(x=>typeof x !== "string").length'), 1, 'the next voice turn sends without touching any button');
});

test('played acknowledgement is sent after completion, never when synthesis is received', async () => {
  const { run } = app();
  await run(`globalThis.events = []; globalThis.started = false; state.active = true;
    state.socket = { readyState: 1, send: x => events.push(JSON.parse(x)) };
    globalThis.fakeSource = { start: () => started = true, disconnect() {} };
    state.context = { decodeAudioData: async () => ({}), createBufferSource: () => { fakeSource.connect = () => {}; return fakeSource; }, destination: {} };
    state.queue.push({ turn_id: 11, chunk_id: 'chunk-11', safety: 'approved', audio_base64: 'AAA=' });
    playNext();`);
  assert.equal(run('started'), true);
  assert.equal(run('events.length'), 0);
  run('fakeSource.onended();');
  const events = JSON.parse(run('JSON.stringify(events)'));
  assert.deepEqual(events, [{ type: 'played', turn_id: 11, chunk_id: 'chunk-11' }]);
});

test('stopping during asynchronous audio decoding prevents later playback or acknowledgement', async () => {
  const { run } = app();
  run(`globalThis.created = false; globalThis.events = []; state.active = true;
    state.socket = { readyState: 1, send: x => events.push(JSON.parse(x)), close() {} };
    state.context = { state: 'running', close: async () => {}, decodeAudioData: () => new Promise(resolve => { globalThis.finishDecode = resolve; }), createBufferSource: () => { created = true; } };
    state.queue.push({ turn_id: 12, chunk_id: 'chunk-12', safety: 'approved', audio_base64: 'AAA=' });
    globalThis.playPromise = playNext(); stopSession(); finishDecode({});`);
  await run('playPromise');
  assert.equal(run('created'), false);
  assert.equal(run('events.some(e => e.type === "played")'), false);
});

test('approved identity greeting follows the audio queue and a late greeting never interrupts a child', () => {
  const { run } = app();
  run(`state.active = true; state.currentTurn = 0;
    handleMessage({type:'greeting',turn_id:0,chunk_id:'greeting',safety:'approved',mime:'audio/wav',audio_base64:'AAA='});`);
  assert.equal(run('state.queue.length'), 1);
  run(`state.queue = []; state.speech = true;
    handleMessage({type:'greeting',turn_id:0,chunk_id:'greeting',safety:'approved',mime:'audio/wav',audio_base64:'AAA='});`);
  assert.equal(run('state.queue.length'), 0);
});

test('moderate speech with syllable gaps interrupts playback within a short voice window', () => {
  const { run } = app();
  run(`globalThis.events = []; state.active = true; state.uiState = 'speaking'; state.playing = true; state.currentTurn = 3;
    state.socket = {readyState:1,send:x=>events.push(JSON.parse(x).type)};
    state.playback = {source:{stop:()=>events.push('local-stop'),disconnect(){}}};
    for (let i=0;i<15;i++) {
      const rms = i%5===3 ? .001 : .03;
      captureFrame({pcm:new Int16Array(320).fill(Math.round(rms*32767)).buffer,rms});
    }`);
  assert.deepEqual(Array.from(run('events')), ['local-stop', 'interrupt']);
  assert.equal(run('state.speech'), true);
  assert.equal(run('state.playing'), false);
  assert.ok(run('state.frames.length') >= 13, 'pre-roll retains the beginning of the interrupted utterance');
});

test('one connected call captures consecutive turns and keeps listening after a fixed safety reply', async () => {
  const { run, elements } = app();
  run(`globalThis.events = []; globalThis.sources = []; handleMessage({type:'session_started',remaining_seconds:600});
    state.socket = {readyState:1, send:x=>events.push(typeof x === 'string' ? JSON.parse(x) : {type:'pcm'})};
    state.context = {destination:{}, decodeAudioData:async()=>({}), createBufferSource:()=>{
      const source = {connect(){}, disconnect(){}, start(){}, stop(){}}; sources.push(source); return source;
    }};
    globalThis.speak = () => {
      for(let i=0;i<30;i++) captureFrame({pcm:new Int16Array(320).fill(3200).buffer,rms:.1});
      for(let i=0;i<70;i++) captureFrame({pcm:new Int16Array(320).buffer,rms:0});
    };`);
  for (let turn = 1; turn <= 3; turn++) {
    run('speak();');
    run(`handleMessage({type:'audio',turn_id:${turn},chunk_id:'reply-${turn}',category:'${turn === 2 ? 'safety_input' : 'answer'}',safety:'approved',mime:'audio/wav',audio_base64:'AAA='});`);
    await new Promise(setImmediate); // Drain asynchronous decoding across the VM context.
    run('sources.at(-1).onended();');
    assert.equal(run('state.uiState'), 'listening', `turn ${turn}, sources=${run('sources.length')}, ignored=${run('[...state.ignoredTurns]')}`);
    assert.equal(run('state.voiceMode'), 'continuous');
    assert.equal(run('state.active'), true);
  }
  assert.equal(run('events.filter(x=>x.type === "pcm").length'), 3);
  assert.equal(run('events.filter(x=>x.type === "played").length'), 3);
  assert.equal(elements.get('ptt-row').hidden, true);
});

test('idle silence never sends audio, interrupts or prompts another turn', () => {
  const { run } = app();
  run(`globalThis.events=[]; state.active=true; state.uiState='listening';
    state.socket={readyState:1,send:x=>events.push(x)};
    for(let i=0;i<6000;i++) captureFrame({pcm:new Int16Array(320).buffer,rms:0});`);
  assert.equal(run('events.length'), 0);
  assert.equal(run('state.misses'), 0);
  assert.equal(run('state.frames.length'), 0);
  assert.ok(run('state.preroll.length') <= 15, 'silence does not grow the recording buffer');
});

test('mute disables the device and worklet, discards pending audio and resumes fresh continuous listening', () => {
  const { run, elements } = app();
  run(`globalThis.events=[]; globalThis.capture=[]; globalThis.track={enabled:true}; state.active=true;
    state.socket={readyState:1,send:x=>events.push(x)}; state.stream={getAudioTracks:()=>[track]};
    state.recorder={port:{postMessage:x=>capture.push(x)}};
    globalThis.frame=(rms,version=state.captureVersion)=>captureFrame({pcm:new Int16Array(320).fill(Math.round(rms*32767)).buffer,rms,version});
    for(let i=0;i<20;i++) frame(.1);
    toggleMute();
    for(let i=0;i<100;i++) frame(.5);
    submitSpeech();`);
  assert.equal(run('track.enabled'), false);
  assert.equal(run('capture.at(-1).enabled'), false);
  assert.equal(run('events.length'), 0);
  assert.equal(run('state.frames.length'), 0);
  assert.equal(elements.get('mute-button').textContent, '取消静音');
  assert.equal(elements.get('mute-button').getAttribute('aria-pressed'), 'true');
  assert.equal(elements.get('mic-indicator').lastChild.textContent, '麦克风已静音');
  run(`globalThis.oldVersion=state.captureVersion; toggleMute();
    for(let i=0;i<30;i++) frame(.5,oldVersion);
    for(let i=0;i<70;i++) frame(0,oldVersion);`);
  assert.equal(run('events.length'), 0, 'queued pre-unmute frames are rejected');
  assert.equal(run('track.enabled'), true);
  assert.equal(run('capture.at(-1).enabled'), true);
  run(`for(let i=0;i<30;i++) frame(.1); for(let i=0;i<70;i++) frame(0);`);
  assert.equal(run('events.filter(x=>typeof x !== "string").length'), 1);
  assert.equal(elements.get('mute-button').getAttribute('aria-pressed'), 'false');
});

test('push-to-talk is an explicit reversible choice and captures only while held', () => {
  const { run, elements } = app();
  run(`globalThis.events=[]; globalThis.track={enabled:true}; state.active=true; state.uiState='listening';
    state.stream={getAudioTracks:()=>[track]}; state.socket={readyState:1,send:x=>events.push(x)};
    globalThis.voice=()=>captureFrame({pcm:new Int16Array(320).fill(3200).buffer,rms:.1});
    setVoiceMode('ptt'); for(let i=0;i<100;i++) voice();`);
  assert.equal(run('track.enabled'), false);
  assert.equal(run('state.speech'), false);
  assert.equal(elements.get('ptt-row').hidden, false);
  run(`startPTT(); for(let i=0;i<30;i++) voice();`);
  assert.equal(run('track.enabled'), true);
  run('releasePTT();');
  assert.equal(run('track.enabled'), false);
  assert.equal(run('events.filter(x=>typeof x !== "string").length'), 1);
  run('setVoiceMode("continuous");');
  assert.equal(run('track.enabled'), true);
  assert.equal(elements.get('ptt-row').hidden, true);
  assert.equal(elements.get('continuous-mode').getAttribute('aria-pressed'), 'true');
});

test('call controls show elapsed time while preserving the server session deadline', () => {
  const { run, elements } = app();
  run(`handleMessage({type:'session_started',remaining_seconds:120}); state.startedAt=Date.now()-65000; updateTime();`);
  assert.equal(elements.get('start-button').querySelector('span').textContent, '通话中');
  assert.equal(elements.get('call-state').textContent, '通话中');
  assert.equal(elements.get('call-time').textContent, '01:05');
  assert.equal(elements.get('time-label').textContent, '还可以玩 2:00');
  run('state.deadline=Date.now()-1; updateTime();');
  assert.equal(run('state.active'), false);
  assert.equal(elements.get('mute-button').disabled, true);
  assert.equal(elements.get('call-time').textContent, '00:00');
});

test('background and offline events hang up and returning never reconnects automatically', () => {
  for (const event of ['background', 'offline']) {
    const { run, documentListeners, windowListeners } = app();
    run(`globalThis.stops=0; state.active=true;
      state.stream={getTracks:()=>[{stop:()=>stops++}]};`);
    if (event === 'background') { run('document.hidden=true;'); documentListeners.get('visibilitychange')(); }
    else windowListeners.get('offline')();
    assert.equal(run('state.active'), false);
    assert.equal(run('stops'), 1);
    run('document.hidden=false;'); documentListeners.get('visibilitychange')();
    assert.equal(run('state.starting'), false);
    assert.equal(run('state.stream'), null);
  }
});
