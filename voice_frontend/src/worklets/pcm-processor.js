// pcm-processor.js
// AudioWorkletProcessor to capture and pass microphone audio to the main thread.

class PCMProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
  }

  process(inputs, outputs) {
    // Get the first audio input from the microphone source.
    const input = inputs[0];

    if (input && input.length > 0) {
      // The first channel of the input is a Float32Array.
      const pcmData = input[0];
      // Send the raw Float32Array back to the main thread for
      // further processing (resampling and encoding).
      this.port.postMessage(pcmData);
    }

    // Return true to keep the processor active and receiving data.
    return true;
  }
}

// Register the processor with the name expected by the `mic.ts` file.
registerProcessor('pcm-processor', PCMProcessor);