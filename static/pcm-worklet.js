/* Keep capture local. Only complete speech turns leave the browser. */
class PCMRecorder extends AudioWorkletProcessor {
  constructor() {
    super();
    this.enabled = false;
    this.version = 0;
    this.port.onmessage = ({ data }) => {
      if (data.type !== 'capture') return;
      this.enabled = !!data.enabled;
      this.version = data.version;
      // Never retain pre-mute audio or a partial PTT utterance on reactivation.
      this.reset();
    };
    this.reset();
  }
  reset() {
    this.frame = new Int16Array(320);
    this.offset = 0;
    this.phase = 0;
    this.sum = 0;
    this.count = 0;
    this.energy = 0;
  }
  process(inputs, outputs) {
    const input = inputs[0]?.[0];
    // The connected output is intentionally silent, so the microphone never echoes.
    for (const output of outputs) for (const channel of output) channel.fill(0);
    if (!this.enabled || !input) return true;
    for (let i = 0; i < input.length; i++) {
      this.sum += input[i];
      this.count++;
      this.phase += 16000;
      if (this.phase >= sampleRate) {
        this.phase -= sampleRate;
        const value = Math.max(-1, Math.min(1, this.sum / this.count));
        this.sum = 0;
        this.count = 0;
        this.frame[this.offset++] = Math.round(value * (value < 0 ? 32768 : 32767));
        this.energy += value * value;
        if (this.offset === this.frame.length) {
          const buffer = this.frame.buffer;
          this.port.postMessage({ pcm: buffer, rms: Math.sqrt(this.energy / this.offset), version: this.version }, [buffer]);
          this.frame = new Int16Array(320);
          this.offset = 0;
          this.energy = 0;
        }
      }
    }
    return true;
  }
}
registerProcessor('pcm-recorder', PCMRecorder);
