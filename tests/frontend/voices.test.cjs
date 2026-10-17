const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../../static/voice-settings.js'), 'utf8');

function voices() {
  const elements = new Map();
  const documentListeners = new Map();
  const windowListeners = new Map();
  function element() {
    const listeners = new Map();
    const classes = new Set();
    return { hidden: false, disabled: false, value: '', checked: false, textContent: '', children: [], files: [],
      listeners, classList: { toggle: (value, on) => on ? classes.add(value) : classes.delete(value), contains: value => classes.has(value) },
      addEventListener: (name, callback) => listeners.set(name, callback),
      replaceChildren() { this.children = []; }, append(...children) { this.children.push(...children); } };
  }
  const context = vm.createContext({
    document: { hidden: false, getElementById: id => { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); },
      createElement: element, addEventListener: (name, callback) => documentListeners.set(name, callback) },
    window: { addEventListener: (name, callback) => windowListeners.set(name, callback) },
    navigator: {}, fetch: async () => { throw new Error('Unexpected request'); },
    Date, Blob, AbortController, setInterval: () => 1, clearInterval() {}, setTimeout: () => 2, clearTimeout() {},
    btoa: value => Buffer.from(value, 'binary').toString('base64'),
  });
  vm.runInContext(source, context);
  const run = code => vm.runInContext(code, context);
  const click = id => elements.get(id).listeners.get('click')?.();
  const submit = id => elements.get(id).listeners.get('submit')?.({ preventDefault() {} });
  const activate = () => run(`voiceState.unlocked = true; applyVoiceCatalog({selected_id:'melo',clone_available:true,
    limits:{min_seconds:3,max_seconds:20,max_custom:3},voices:[
      {id:'melo',name:'默认',kind:'builtin',available:true},
      {id:'preset',name:'明亮',kind:'builtin',available:true},
      {id:'broken',name:'旧音色',kind:'custom',available:false}]});`);
  return { run, elements, activate, click, submit, documentListeners, windowListeners };
}
const flush = () => new Promise(setImmediate);

test('voice tools start locked without accessing microphone or loading private voice metadata', () => {
  const { run, elements } = voices();
  assert.equal(run('voiceState.unlocked'), false);
  assert.equal(elements.get('voice-settings-panel').hidden, true);
  assert.equal(elements.get('voice-record-start').disabled, true);
  assert.equal(run('voiceState.stream'), null);
  assert.equal(run('voiceState.context'), null);
});

test('local WAV conversion produces 16 kHz mono PCM16, downmixes and clamps samples', () => {
  const { run } = voices();
  const bytes = run(`encodeVoiceWav({duration:3, sampleRate:48000, length:144000, numberOfChannels:2,
    getChannelData:index=>new Float32Array(144000).fill(index ? -.5 : 1)})`);
  const view = new DataView(bytes);
  assert.equal(bytes.byteLength, 44 + 3 * 16000 * 2);
  assert.equal(Buffer.from(bytes).subarray(0, 4).toString(), 'RIFF');
  assert.equal(view.getUint16(22, true), 1);
  assert.equal(view.getUint32(24, true), 16000);
  assert.equal(view.getUint16(34, true), 16);
  assert.equal(view.getInt16(44, true), 8192);
  const clipped = new DataView(run(`encodeVoiceWav({duration:.02,sampleRate:16000,length:320,numberOfChannels:1,getChannelData:()=>new Float32Array(320).fill(-2)})`));
  assert.equal(clipped.getInt16(44, true), -32768);
});

test('file duration and size are rejected locally without uploading or retaining audio', async () => {
  const { run, activate } = voices(); activate();
  run(`globalThis.duration=2; globalThis.decoded=0;
    voiceState.context={state:'running',decodeAudioData:async()=>{decoded++;return {duration};}};`);
  await assert.rejects(run('prepareVoiceSample(new ArrayBuffer(10),voiceState.sampleEpoch)'), /3–20/);
  run('duration=21;');
  await assert.rejects(run('prepareVoiceSample(new ArrayBuffer(10),voiceState.sampleEpoch)'), /3–20/);
  await assert.rejects(run('prepareVoiceSample(new ArrayBuffer(10*1024*1024+1),voiceState.sampleEpoch)'), /10 MB/);
  assert.equal(run('decoded'), 2, 'oversized data is rejected before decoding');
  assert.equal(run('voiceState.sample'), null);
});

test('recording needs the adult permission checkbox and cancellation retires a pending microphone grant', async () => {
  const { run, activate, elements } = voices(); activate();
  run(`globalThis.requested=0; globalThis.stopped=0;
    window.MediaRecorder=function(){};
    navigator.mediaDevices={getUserMedia:()=>{requested++;return new Promise(resolve=>globalThis.grant=resolve);}};`);
  await run('startVoiceRecording();');
  assert.equal(run('requested'), 0);
  elements.get('voice-consent').checked = true;
  run('globalThis.capture=startVoiceRecording();');
  assert.equal(run('requested'), 1);
  run(`stopVoiceSettings(); grant({getTracks:()=>[{stop:()=>stopped++}]});`);
  await run('capture');
  assert.equal(run('stopped'), 1);
  assert.equal(run('voiceState.stream'), null);
  assert.equal(run('voiceState.recorder'), null);
  assert.equal(elements.get('voice-consent').checked, false);
});

test('closing the parent panel stops active tracks, recorder and preview and clears unsaved voice data', () => {
  const { run, activate, elements } = voices(); activate();
  run(`globalThis.events=[]; voiceState.recording=true; voiceState.sample=new ArrayBuffer(100); voiceState.chunks=[new Blob(['test'])];
    voiceState.stream={getTracks:()=>[{stop:()=>events.push('track')}]};
    voiceState.recorder={state:'recording',stop:()=>events.push('recorder'),onstop:()=>{throw Error('must be removed');}};
    voiceState.playback={stop:()=>events.push('playback'),disconnect:()=>events.push('disconnect'),onended:()=>{}};
    voiceState.context={state:'running',close:()=>{events.push('context');return Promise.resolve();}};`);
  elements.get('voice-name').value = '参考名称';
  elements.get('voice-reference-text').value = '需要清除的逐字稿';
  elements.get('parent-dialog').listeners.get('close')();
  assert.deepEqual(Array.from(run('events')), ['recorder', 'track', 'playback', 'disconnect', 'context']);
  assert.equal(run('voiceState.sample'), null);
  assert.equal(run('voiceState.chunks.length'), 0);
  assert.equal(run('voiceState.context'), null);
  assert.equal(elements.get('voice-name').value, '');
  assert.equal(elements.get('voice-reference-text').value, '');
});

test('a delayed file read after cleanup cannot reopen the audio context or restore a sample', async () => {
  const { run, activate, elements } = voices(); activate();
  elements.get('voice-consent').checked = true;
  run(`globalThis.contexts=0; window.AudioContext=function(){contexts++;throw new Error('late context');};
    voiceElement('voice-file').files=[{size:10,arrayBuffer:()=>new Promise(resolve=>globalThis.finishFile=resolve)}];
    globalThis.reading=chooseVoiceFile(); stopVoiceSettings(); finishFile(new ArrayBuffer(10));`);
  await run('reading');
  assert.equal(run('contexts'), 0);
  assert.equal(run('voiceState.sample'), null);
});

test('a delayed decode from a cleared sample cannot restore reference audio', async () => {
  const { run, activate } = voices(); activate();
  run(`voiceState.context={state:'running',decodeAudioData:()=>new Promise(resolve=>globalThis.finishDecode=resolve)};
    globalThis.decoding=prepareVoiceSample(new ArrayBuffer(10),voiceState.sampleEpoch);
    clearVoiceSample();finishDecode({duration:5});`);
  await run('decoding');
  assert.equal(run('voiceState.sample'), null);
});

test('select uses the authenticated voice API and keeps controls disabled while preparing', async () => {
  const { run, activate, elements, submit } = voices(); activate();
  elements.get('voice-select').value = 'preset';
  run(`globalThis.requests=[]; fetch=(url,options)=>{requests.push({url,...options});return new Promise(resolve=>globalThis.complete=resolve);};`);
  submit('voice-select-form');
  assert.equal(elements.get('voice-save').textContent, '正在准备音色…');
  assert.equal(elements.get('voice-select').disabled, true);
  assert.equal(run('requests[0].url'), '/api/voices/select');
  assert.equal(run('requests[0].credentials'), 'same-origin');
  assert.deepEqual(JSON.parse(run('requests[0].body')), { voice_id: 'preset' });
  run(`complete({ok:true,json:async()=>({...voiceState.catalog,selected_id:'preset'})});`);
  await flush();
  assert.equal(run('voiceState.catalog.selected_id'), 'preset');
  assert.equal(elements.get('voice-select').disabled, false);
  assert.match(elements.get('voice-message').textContent, /下次拨打/);
});

test('preview resumes WebAudio from the click and closing before the response prevents playback', async () => {
  const { run, activate, click } = voices(); activate();
  run(`globalThis.events=[]; globalThis.sources=0;
    voiceState.context={state:'running',resume:()=>{events.push('resume');return Promise.resolve();},
      close:()=>{events.push('close');return Promise.resolve();},decodeAudioData:async()=>({}),
      createBufferSource:()=>{sources++;throw Error('late playback');}};
    fetch=(url,options)=>{events.push('fetch');return new Promise(resolve=>globalThis.complete=resolve);};`);
  click('voice-preview');
  await flush();
  assert.deepEqual(Array.from(run('events')), ['resume', 'fetch']);
  run(`stopVoiceSettings(); complete({ok:true,headers:{get:()=>'audio/wav'},arrayBuffer:async()=>new ArrayBuffer(8)});`);
  await flush();
  assert.equal(run('sources'), 0);
  assert.equal(run('voiceState.playback'), null);
  assert.equal(run('voiceState.context'), null);
});

test('expired parent authorization clears the sample and requests parent unlock again', async () => {
  const { run, activate, elements, submit } = voices(); activate();
  elements.get('voice-select').value = 'preset';
  run(`globalThis.expired=0;voiceState.onExpired=()=>expired++;voiceState.sample=new ArrayBuffer(32);
    fetch=async()=>({ok:false,status:403,json:async()=>({detail:'parent expired'})});`);
  submit('voice-select-form');
  await flush();
  assert.equal(run('expired'), 1);
  assert.equal(run('voiceState.unlocked'), false);
  assert.equal(run('voiceState.sample'), null);
  assert.equal(elements.get('voice-settings-panel').hidden, true);
});

test('creating a voice sends only explicit consent and canonical reference data then clears the local sample', async () => {
  const { run, activate, elements, submit } = voices(); activate();
  elements.get('voice-name').value = '参考声音';
  elements.get('voice-reference-text').value = '这是一段参考录音。';
  elements.get('voice-consent').checked = true;
  run(`voiceState.sample=encodeVoiceWav({duration:3,sampleRate:16000,length:48000,numberOfChannels:1,getChannelData:()=>new Float32Array(48000)});
    globalThis.requests=[];fetch=async(url,options)=>{requests.push({url,...options});return {ok:true,json:async()=>({...voiceState.catalog,created_id:'new',
      voices:[...voiceState.catalog.voices,{id:'new',name:'参考声音',kind:'custom',available:true}]})};};`);
  submit('voice-clone-form');
  await flush();
  const body = JSON.parse(run('requests[0].body'));
  assert.equal(body.consent, true);
  assert.equal(body.reference_text, '这是一段参考录音。');
  assert.equal(Buffer.from(body.audio_base64, 'base64').subarray(0, 4).toString(), 'RIFF');
  assert.equal(run('voiceState.sample'), null);
  assert.equal(elements.get('voice-consent').checked, false);
  assert.equal(elements.get('voice-select').value, 'new');
  assert.equal(run('voiceState.catalog.selected_id'), 'melo', 'creation does not silently change the selected voice');
});

test('an unavailable custom voice can still be deleted', async () => {
  const { run, activate, elements, click } = voices(); activate();
  elements.get('voice-select').value = 'broken';
  run(`renderVoiceControls();globalThis.requests=[];fetch=async(url,options)=>{requests.push({url,...options});return {ok:true,json:async()=>({...voiceState.catalog,voices:voiceState.catalog.voices.filter(v=>v.id!=='broken')})};};`);
  assert.equal(elements.get('voice-preview').disabled, true);
  assert.equal(elements.get('voice-delete').disabled, false);
  click('voice-delete'); await flush();
  assert.equal(run('requests[0].method'), 'DELETE');
  assert.equal(run('requests[0].url'), '/api/voices/broken');
});
