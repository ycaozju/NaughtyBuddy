const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../../static/pcm-worklet.js'), 'utf8');

function recorder(rate) {
  const frames = [];
  let Processor;
  class AudioWorkletProcessor {
    constructor() { this.port = { postMessage: (message) => frames.push(message) }; }
  }
  vm.runInNewContext(source, { AudioWorkletProcessor, sampleRate: rate, registerProcessor: (_, value) => { Processor = value; } });
  const instance = new Processor();
  instance.port.onmessage({ data: { type: 'capture', enabled: true, version: 1 } });
  return { instance, frames };
}
for (const rate of [16000, 44100, 48000]) {
  test(`${rate} Hz microphone capture produces exact 16 kHz PCM16 without audible monitoring`, () => {
    const { instance, frames } = recorder(rate);
    const signal = Float32Array.from({ length: rate }, (_, i) => .4 * Math.sin(2 * Math.PI * 220 * i / rate));
    for (let start = 0; start < signal.length; start += 128) {
      const output = new Float32Array(128).fill(1);
      assert.equal(instance.process([[signal.subarray(start, start + 128)]], [[output]]), true);
      assert.equal(output.every((sample) => sample === 0), true);
    }
    assert.equal(frames.length, 50, 'one second is exactly fifty 20ms frames');
    const pcm = frames.flatMap((frame) => Array.from(new Int16Array(frame.pcm)));
    assert.equal(pcm.length, 16000);
    assert.ok(Math.max(...pcm) > 12000);
    assert.ok(Math.min(...pcm) < -12000);
    assert.ok(frames.every((frame) => frame.rms > .25 && frame.rms < .31));
  });
}
test('silence stays silent, and over-range samples do not wrap PCM16', () => {
  const { instance, frames } = recorder(16000);
  instance.process([[new Float32Array(320)]], []);
  assert.equal(frames[0].rms, 0);
  assert.ok(new Int16Array(frames[0].pcm).every((sample) => sample === 0));
  instance.process([[new Float32Array(320).fill(2)]], []);
  assert.ok(new Int16Array(frames[1].pcm).every((sample) => sample === 32767));
  instance.process([[new Float32Array(320).fill(-2)]], []);
  assert.ok(new Int16Array(frames[2].pcm).every((sample) => sample === -32768));
});

test('muted capture emits no PCM and discards partial pre-mute samples on resume', () => {
  const { instance, frames } = recorder(16000);
  instance.process([[new Float32Array(160).fill(-.5)]], []);
  instance.port.onmessage({ data: { type: 'capture', enabled: false, version: 2 } });
  instance.process([[new Float32Array(16000).fill(-.5)]], []);
  assert.equal(frames.length, 0);
  instance.port.onmessage({ data: { type: 'capture', enabled: true, version: 3 } });
  instance.process([[new Float32Array(320).fill(.25)]], []);
  assert.equal(frames.length, 1);
  assert.equal(frames[0].version, 3);
  assert.ok(new Int16Array(frames[0].pcm).every(sample => sample > 0), 'only post-unmute audio is included');
});
