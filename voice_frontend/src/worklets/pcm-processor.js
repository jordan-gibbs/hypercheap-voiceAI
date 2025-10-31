// Input (e.g. 48 kHz) float -> 16 kHz PCM16, batched efficiently in the worklet.
class PCMProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buf = new Float32Array(0);
    // FIX: Increased target chunk size from 20ms (320 samples) to 250ms (4000 samples).
    this.targetOutSamples = 1000; // 62.5ms @ 16k
  }
  // simple linear resampler
  resampleTo16k(f32) {
    const ratio = sampleRate / 16000; // sampleRate is global in AW
    const outLen = Math.floor(f32.length / ratio);
    const out = new Int16Array(outLen);
    for (let i = 0; i < outLen; i++) {
      const idx = i * ratio, i0 = Math.floor(idx), i1 = Math.min(i0 + 1, f32.length - 1);
      const s = f32[i0] * (1 - (idx - i0)) + f32[i1] * (idx - i0);
      const v = Math.max(-1, Math.min(1, s));
      out[i] = v < 0 ? v * 0x8000 : v * 0x7FFF;
    }
    return out;
  }
  process(inputs) {
    const ch0 = inputs?.[0]?.[0];
    if (ch0) {
      // append into a growing buffer at the input sample rate
      const merged = new Float32Array(this.buf.length + ch0.length);
      merged.set(this.buf, 0);
      merged.set(ch0, this.buf.length);
      this.buf = merged;

      // Calculate required input samples dynamically based on the actual sample rate.
      // E.g., 4000 samples @ 16k requires (48000/16000) * 4000 = 12000 samples @ 48k.
      const ratio = sampleRate / 16000;
      const requiredInputSamples = Math.ceil(this.targetOutSamples * ratio);

      // when enough input is available, emit a chunk
      while (this.buf.length >= requiredInputSamples) {
        const slice = this.buf.subarray(0, requiredInputSamples);
        this.buf = this.buf.subarray(requiredInputSamples);
        const pcm = this.resampleTo16k(slice);
        // Transfer the underlying ArrayBuffer (zero-copy)
        this.port.postMessage(pcm.buffer, [pcm.buffer]);
      }
    }
    return true;
  }
}
registerProcessor('pcm-processor', PCMProcessor);