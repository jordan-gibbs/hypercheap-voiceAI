class PCMPlayer extends AudioWorkletProcessor {
  constructor() {
    super();
    this.queue = [];
    this.ptr = 0;
    this.isPlaying = false; // Track playback state

    // Correctly assign the onmessage handler
    this.port.onmessage = (e) => {
      const { type, buffer, byteLength } = e.data || {};
      if (type === 'push' && buffer) {
        // Ensure even length (16-bit samples)
        const len = (byteLength ?? buffer.byteLength) & ~1;
        // Reinterpret transferred PCM16 little-endian data as Int16 samples
        this.queue.push(new Int16Array(buffer, 0, len >> 1));

        // If we were not playing and now have data, signal start
        if (!this.isPlaying && this.queue.length > 0) {
            this.isPlaying = true;
            this.port.postMessage({ type: 'state', isPlaying: true });
        }

      } else if (type === 'clear') {
        // Handle clear command for barge-in
        this.queue = [];
        this.ptr = 0;
        // If we were playing and now cleared, signal stop
        if (this.isPlaying) {
            this.isPlaying = false;
            this.port.postMessage({ type: 'state', isPlaying: false });
        }
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

      // int16 → float32 in [-1, 1]
      out[i++] = Math.max(-1, Math.min(1, cur[this.ptr++] / 32768));
    }

    // Check for buffer underrun (playback finished)
    // If we are playing, the queue is empty, AND we didn't fill the output buffer:
    if (this.isPlaying && this.queue.length === 0 && i < out.length) {
        this.isPlaying = false;
        this.port.postMessage({ type: 'state', isPlaying: false });
    }

    // Fill remaining buffer with silence
    for (; i < out.length; i++) {
      out[i] = 0;
    }
    
    return true;
  }
}

registerProcessor('pcm-player', PCMPlayer);