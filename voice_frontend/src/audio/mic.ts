import { resampleAndEncodePCM16 } from './resample'
import processorCode from '../worklets/pcm-processor.js?raw'

export async function startMic() {
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    throw new Error('getUserMedia not supported or insecure context (use https or localhost)')
  }

  const stream = await navigator.mediaDevices.getUserMedia({
    audio: {
      channelCount: 1,
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
    }
  })

  const ctx = new (window.AudioContext || (window as any).webkitAudioContext)({ sampleRate: 48000 })

  // Create a Blob from the raw code and generate a local URL
  const processorBlob = new Blob([processorCode], { type: 'application/javascript' })
  const processorUrl = URL.createObjectURL(processorBlob)

  await ctx.audioWorklet.addModule(processorUrl)
  URL.revokeObjectURL(processorUrl) // Clean up the blob URL after it's been used


  const source = ctx.createMediaStreamSource(stream)
  const node = new AudioWorkletNode(ctx, 'pcm-processor')

  source.connect(node)
  // Do not connect to destination to avoid loopback

  let floatQueue: Float32Array[] = []
  const listeners: Array<(bytes: Uint8Array) => void> = []

  node.port.onmessage = (event) => {
    const chunk = event.data as Float32Array
    floatQueue.push(chunk)

    // Aim ~32ms windows
    const targetWindow = Math.floor(0.032 * ctx.sampleRate)
    let total = 0
    for (const f of floatQueue) total += f.length
    if (total >= targetWindow) {
      const merged = new Float32Array(total)
      let o = 0
      for (const f of floatQueue) { merged.set(f, o); o += f.length }
      floatQueue = []

      const bytes = resampleAndEncodePCM16(merged, ctx.sampleRate, 16000)
      for (const fn of listeners) fn(bytes)
    }
  }

  return {
    stream,
    sampleRate: ctx.sampleRate,
    stop: () => {
      stream.getTracks().forEach(t => t.stop())
      node.disconnect()
      source.disconnect()
      ctx.close()
    },
    onAudio: (cb: (bytes: Uint8Array) => void) => {
      listeners.push(cb)
      return () => {
        const i = listeners.indexOf(cb)
        if (i >= 0) listeners.splice(i, 1)
      }
    }
  }
}