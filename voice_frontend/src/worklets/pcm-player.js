// src/worklets/pcm-player.js
class PCMPlayer extends AudioWorkletProcessor {
  constructor() {
    super();
    this.queue = [];
    this.ptr = 0;
    this.isPlaying = false;

    // volume ramp state
    this.gain = 1.0;            // current linear gain
    this.rampSamplesLeft = 0;   // samples remaining in the fade
    this.rampStep = 0;          // delta gain per sample

    this.port.onmessage = (e) => {
      const { type, buffer, byteLength } = e.data || {};

      if (type === 'push' && buffer) {
        // Ensure even byte length (16-bit)
        const len = (byteLength ?? buffer.byteLength) & ~1;
        this.queue.push(new Int16Array(buffer, 0, len >> 1));

        // entering playing state
        if (!this.isPlaying && this.queue.length > 0) {
          this.isPlaying = true;
          this.port.postMessage({ type: 'state', isPlaying: true });
        }

        // if we previously faded to 0, restore for new playback
        if (this.gain === 0) {
          this.gain = 1.0;
          this.rampSamplesLeft = 0;
          this.rampStep = 0;
        }

      } else if (type === 'clear') {
        // immediate stop & reset (used on hard cuts)
        this.queue = [];
        this.ptr = 0;
        this.rampSamplesLeft = 0;
        this.rampStep = 0;
        // keep current gain (caller may ramp again later)
        if (this.isPlaying) {
          this.isPlaying = false;
          this.port.postMessage({ type: 'state', isPlaying: false });
        }

      } else if (type === 'ramp_down') {
        const ms = Math.max(1, e.data?.ms ?? 500);
        // Ignore if we're already fading or effectively silent
        if (this.rampSamplesLeft > 0 || this.gain <= 0.0001) return;
        this.rampSamplesLeft = Math.max(1, Math.floor(sampleRate * (ms / 1000)));
        this.rampStep = (0 - this.gain) / this.rampSamplesLeft; // linear to 0
      }
    };
  }

  process(_inputs, outputs) {
    const out = outputs[0][0]; // mono
    let i = 0;

    while (i < out.length) {
      if (this.queue.length === 0) break;
      const cur = this.queue[0];

      if (this.ptr >= cur.length) {
        this.queue.shift();
        this.ptr = 0;
        continue;
      }

      // int16 -> float32 in [-1, 1]
      let s = Math.max(-1, Math.min(1, cur[this.ptr++] / 32768));

      // apply per-sample ramp (if active)
      if (this.rampSamplesLeft > 0) {
        this.gain += this.rampStep;
        this.rampSamplesLeft--;
        if (this.rampSamplesLeft === 0) {
          // clamp final gain
          this.gain = Math.min(1, Math.max(0, this.gain));
          if (this.gain <= 0.0001) {
            this.gain = 0;
            // -- FIX: DO NOT CLEAR THE QUEUE HERE --
            if (this.isPlaying) {
              this.isPlaying = false;
              this.port.postMessage({ type: 'state', isPlaying: false });
            }
          }
        }
      }

      out[i++] = s * this.gain;

      // if we reached effective silence, stop emitting more samples this frame
      if (this.gain <= 0) break;
    }

    // underrun detection (normal end of buffered audio)
    // **THE FIX**: This condition is now more robust. It triggers as soon as the queue is empty,
    // resolving the edge case where the last audio sample perfectly filled a processing frame.
    if (this.isPlaying && this.queue.length === 0 && this.gain > 0) {
      this.isPlaying = false;
      this.port.postMessage({ type: 'state', isPlaying: false });
    }

    // fill remainder with silence
    for (; i < out.length; i++) out[i] = 0;

    return true;
  }
}

registerProcessor('pcm-player', PCMPlayer);