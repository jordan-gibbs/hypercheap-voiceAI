import { startMic } from '../audio/mic'
import playerCode from '../worklets/pcm-player.js?raw'

const WS_URL = import.meta.env.VITE_AGENT_WS_URL || 'ws://localhost:8000/ws/agent'

// Build a WAV header for PCM16 mono @ sampleRate (Kept for fallback mechanism)
function wavHeader(dataBytes: number, sampleRate: number, channels = 1, bits = 16): Uint8Array {
  const blockAlign = channels * (bits >> 3)
  const byteRate = sampleRate * blockAlign
  const buffer = new ArrayBuffer(44)
  const view = new DataView(buffer)
  const w = (o: number, s: string) => { for (let i = 0; i < s.length; i++) view.setUint8(o + i, s.charCodeAt(i)) }
  w(0, 'RIFF')
  view.setUint32(4, 36 + dataBytes, true)
  w(8, 'WAVE')
  w(12, 'fmt ')
  view.setUint32(16, 16, true)
  view.setUint16(20, 1, true)
  view.setUint16(22, channels, true)
  view.setUint32(24, sampleRate, true)
  view.setUint32(28, byteRate, true)
  view.setUint16(32, blockAlign, true)
  view.setUint16(34, bits, true)
  w(36, 'data')
  view.setUint32(40, dataBytes, true)
  return new Uint8Array(buffer)
}

let playerCtx: AudioContext | null = null
let playerNode: AudioWorkletNode | null = null
// Global listeners for playback state changes (Subscription model)
let playbackListeners: Array<(isPlaying: boolean) => void> = []

// Backend streams LINEAR16 @ 48k by default; browser may override our request.
const DEFAULT_TTS_SAMPLE_RATE = 48000

/**
 * Create & resume the playback AudioContext (must be called from a user gesture).
 */
export async function primePlayer() {
  if (!playerCtx) {
    playerCtx = new (window.AudioContext || (window as any).webkitAudioContext)({ sampleRate: DEFAULT_TTS_SAMPLE_RATE })

    // Create a Blob from the raw code and generate a local URL
    const playerBlob = new Blob([playerCode], { type: 'application/javascript' })
    const playerUrl = URL.createObjectURL(playerBlob)

    await playerCtx.audioWorklet.addModule(playerUrl)
    URL.revokeObjectURL(playerUrl) // Clean up the blob URL after it's been used

    playerNode = new AudioWorkletNode(playerCtx, 'pcm-player')

    // Listen for playback state changes from the worklet and dispatch to subscribers
    playerNode.port.onmessage = (e) => {
        if (e.data.type === 'state') {
            const isPlaying = e.data.isPlaying;
            for (const listener of playbackListeners) {
                listener(isPlaying);
            }
        }
    }

    playerNode.connect(playerCtx.destination)
  }
  if (playerCtx.state !== 'running') {
    await playerCtx.resume()
  }
}

// ... (the rest of the file is unchanged)
function onPlaybackState(cb: (isPlaying: boolean) => void) {
    playbackListeners.push(cb);
    return () => {
        const index = playbackListeners.indexOf(cb);
        if (index > -1) {
            playbackListeners.splice(index, 1);
        }
    }
}

type Handlers = {
  onAsr: (text: string) => void
  onStatus: (status: string) => void
  onToken: (tok: string) => void
  onSegment: (audio: Blob) => void
  onTurnDone: () => void // NEW
  onDone: (final: Blob | null) => void
  onPlaybackState: (isPlaying: boolean) => void // NEW
}

export async function connectAndRecord(h: Handlers) {
  // Start mic immediately; if not a secure context, this will throw.
  const { stop: stopMic, onAudio } = await startMic()

  // Subscribe to playback state changes for this session
  const unsubPlayback = onPlaybackState(h.onPlaybackState);

  const ws = new WebSocket(WS_URL)
  ws.binaryType = 'arraybuffer' // ensure e.data is ArrayBuffer for binary frames

  // If player isn’t primed, we’ll accumulate PCM and hand back WAV to the UI (fallback).
  let seg: Uint8Array[] = []
  let closed = false

  const finalizeSegment = () => {
    const total = seg.reduce((n, a) => n + a.byteLength, 0);
    if (total > 0) {
      const sr = playerCtx?.sampleRate ?? DEFAULT_TTS_SAMPLE_RATE;
      const header = wavHeader(total, sr, 1, 16);
      // Use 'as BlobPart[]' to fix the type error
      const blob = new Blob([header, ...seg] as BlobPart[], { type: 'audio/wav' });
      seg = [];
      h.onSegment(blob);
    } else {
      // Always pass a Blob, even if empty. This fixes the 'null' is not assignable error.
      h.onSegment(new Blob([]));
    }
  };

  const finalizeAll = () => {
    const total = seg.reduce((n, a) => n + a.byteLength, 0);
    const sr = playerCtx?.sampleRate ?? DEFAULT_TTS_SAMPLE_RATE;
    // Use 'as BlobPart[]' here as well
    const blob = total ? new Blob([wavHeader(total, sr, 1, 16), ...seg] as BlobPart[], { type: 'audio/wav' }) : null;
    seg = [];
    h.onDone(blob);
  };


  ws.onopen = async () => {
    // Synchronization: Send 'start' to initiate backend initialization.
    try { ws.send(JSON.stringify({ type: 'start' })) } catch {}
    // If the player exists but was suspended, try to resume.
    try { await playerCtx?.resume() } catch {}
  }

  ws.onmessage = (e) => {
    // JSON events
    if (typeof e.data === 'string') {
      try {
        const msg = JSON.parse(e.data)
        switch (msg.type) {
          case 'status':
            h.onStatus(msg.message)
            break
          case 'asr_final':
            // BARGE-IN: When a new user utterance arrives, immediately stop current playback.
            if (playerNode) {
              playerNode.port.postMessage({ type: 'clear' });
            }
            // Also clear any accumulated segments in the fallback path
            seg = [];

            h.onAsr(msg.text)
            break
          case 'llm_token':
            h.onToken(msg.text)
            break
          case 'segment_done':
            finalizeSegment()
            break
          case 'turn_done': // NEW
            h.onTurnDone()
            break
          case 'done':
            // Server confirmed session end.
            cleanup();
            break
        }
      } catch {
        // ignore parse errors
      }
      return
    }

    // Binary frames: raw PCM16 little-endian (ArrayBuffer due to binaryType)
    const buf: ArrayBuffer = e.data as ArrayBuffer

    if (playerNode) {
      // Stream to AudioWorklet with zero-copy by transferring the ArrayBuffer.
      // Also pass explicit byteLength so the Worklet can ignore any trailing odd byte.
      playerNode.port.postMessage(
        { type: 'push', buffer: buf, byteLength: buf.byteLength },
        [buf] // transfer ownership
      )
      // IMPORTANT: do NOT also push to seg; the buffer is transferred (neutered).
    } else {
      // Fallback (player not primed yet): accumulate a copy for WAV stitching.
      // Copy so we keep ownership (no transfer in this branch).
      seg.push(new Uint8Array(buf.slice(0)))
    }
  }

  const cleanup = () => {
    if (!closed) {
        closed = true;
        try { ws.close() } catch {}
        stopMic();
        if (unsub) unsub(); // Unsubscribe mic audio
        unsubPlayback(); // Unsubscribe playback state

        // Clear player on session end
        if (playerNode) {
            playerNode.port.postMessage({ type: 'clear' });
        }

        // Best-effort finalize anything we had
        finalizeAll();
    }
  }

  ws.onerror = (e) => {
    console.error("WebSocket error:", e);
    h.onStatus("error");
    cleanup();
  }
  ws.onclose = cleanup;

  const unsub = onAudio((bytes: Uint8Array) => {
    if (ws.readyState === WebSocket.OPEN) {
      // If the socket is congested, you could drop or coalesce mic frames here:
      // if (ws.bufferedAmount > 512_000) return;
      ws.send(bytes)
    }
  })

  return {
    stop: async () => {
      if (closed) return;
      // Send stop signal and rely on cleanup() being called when 'done' arrives or WS closes.
      try { ws.send(JSON.stringify({ type: 'stop' })) } catch {}

      // Force-close after a grace period in case the server never answers.
      setTimeout(() => {
        if (!closed) {
            console.warn("Timeout waiting for server 'done' event, forcing cleanup.");
            cleanup();
        }
      }, 5000)
    }
  }
}