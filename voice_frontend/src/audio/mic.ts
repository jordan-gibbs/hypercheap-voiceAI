import processorCode from '../worklets/pcm-processor.js?raw'

/**
 * Start mic capture and emit ~20ms chunks of PCM16 @ 16kHz (little-endian).
 * We do resampling inside an AudioWorklet for low latency and zero GC churn.
 */
export async function startMic() {
  if (!navigator.mediaDevices?.getUserMedia) {
    throw new Error('getUserMedia not supported or insecure context (use https or localhost)')
  }

  // Ask the browser for a clean, single-channel capture; let OS do EC/NS/AGC if needed
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: {
      channelCount: 1,
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
    },
  })

  // Interactive latency; 48k is typical input rate on most devices
  const ctx = new (window.AudioContext || (window as any).webkitAudioContext)({
    sampleRate: 48000,
    latencyHint: 'interactive',
  })

  // Load the worklet from a blob (works in Vite/Prod without public file)
  const blob = new Blob([processorCode], { type: 'application/javascript' })
  const url = URL.createObjectURL(blob)
  await ctx.audioWorklet.addModule(url)
  URL.revokeObjectURL(url)

  const source = ctx.createMediaStreamSource(stream)
  const node = new AudioWorkletNode(ctx, 'pcm-processor')
  source.connect(node) // no loopback

  const listeners: Array<(bytes: Uint8Array) => void> = []

  node.port.onmessage = (event: MessageEvent) => {
    // Worklet posts ArrayBuffers that are already PCM16 LE @ 16kHz
    const data = event.data
    if (data instanceof ArrayBuffer) {
      const bytes = new Uint8Array(data)
      for (const fn of listeners) fn(bytes)
    }
  }

  let closed = false
  return {
    stream,
    sampleRate: 16000,
    stop: () => {
      if (closed) return
      closed = true
      try { stream.getTracks().forEach(t => t.stop()) } catch {}
      try { node.disconnect() } catch {}
      try { source.disconnect() } catch {}
      try { ctx.close() } catch {}
    },
    onAudio: (cb: (bytes: Uint8Array) => void) => {
      listeners.push(cb)
      return () => {
        const i = listeners.indexOf(cb)
        if (i >= 0) listeners.splice(i, 1)
      }
    },
  }
}
