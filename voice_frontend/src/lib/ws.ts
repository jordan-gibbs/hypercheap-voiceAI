// src/lib/ws.ts
// One WebSocket. AudioWorklet-only playback. Bounded-latency send path.

import { startMic } from '../audio/mic'
import playerCode from '../worklets/pcm-player.js?raw'

const WS_URL = import.meta.env.VITE_AGENT_WS_URL || 'ws://localhost:8000/ws/agent'
const RAMP_MS = 250

// ----- VAD tuning & debug -----
// Turn this on to see logs in your browser console:
const DEBUG_VAD = true
const VAD_MIN_PROB = 0.6      // tune 0.55–0.7 if needed (Silero threshold is ~0.5 default)
const VAD_MIN_FRAMES = 2      // debounce: consecutive speech-like frames before we consider "speech"
const VAD_CLEAR_FRAMES = 2    // consecutive non-speech frames before we consider "not speaking"

// --- VAD utils ---
function vadProb(msg: any): number | null {
  const p = msg?.prob ?? msg?.probability ?? msg?.p ?? msg?.activation ?? msg?.score
  return (typeof p === 'number') ? p : null
}

function vadIsSpeechLike(msg: any): boolean {
  const s = (msg?.state ?? '').toLowerCase()
  const p = vadProb(msg)

  // Prefer explicit state when present
  if (s === 'speech')   return p == null ? true : p >= VAD_MIN_PROB
  if (s === 'silence')  return false
  if (s === 'noise')    return false

  // No explicit state? Fall back to probability-only
  return p != null ? p >= VAD_MIN_PROB : false
}

// Throttled logger to avoid spamming (main-thread logs; worklet logs don't always show)
let _lastVadLog = 0
function logVad(...args: any[]) {
  if (!DEBUG_VAD) return
  const now = performance.now()
  if (now - _lastVadLog > 200) {
    _lastVadLog = now
    // eslint-disable-next-line no-console
    console.log('[VAD]', ...args)
  }
}

// Playback graph (AudioWorklet). We stream raw PCM16 @ 48k directly to it.
let playerCtx: AudioContext | null = null
let playerNode: AudioWorkletNode | null = null

// Playback state subscribers (to drive UI state like "speaking")
let playbackListeners: Array<(isPlaying: boolean) => void> = []
export function onPlaybackState(cb: (isPlaying: boolean) => void) {
  playbackListeners.push(cb)
  return () => {
    const i = playbackListeners.indexOf(cb)
    if (i >= 0) playbackListeners.splice(i, 1)
  }
}

const DEFAULT_TTS_SAMPLE_RATE = 48000

export async function primePlayer() {
  if (!playerCtx) {
    playerCtx = new (window.AudioContext || (window as any).webkitAudioContext)({
      sampleRate: DEFAULT_TTS_SAMPLE_RATE,
    })

    const blob = new Blob([playerCode], { type: 'application/javascript' })
    const url = URL.createObjectURL(blob)
    await playerCtx.audioWorklet.addModule(url)
    URL.revokeObjectURL(url)

    playerNode = new AudioWorkletNode(playerCtx, 'pcm-player')
    playerNode.port.onmessage = (e) => {
      if (e.data?.type === 'state') {
        const isPlaying = !!e.data.isPlaying
        for (const fn of playbackListeners) fn(isPlaying)
      }
    }
    playerNode.connect(playerCtx.destination)
  }
  if (playerCtx.state !== 'running') {
    await playerCtx.resume()
  }
}

type Handlers = {
  onAsr: (text: string) => void
  onStatus: (status: string) => void
  onToken: (tok: string) => void
  onTurnDone: () => void
  onDone: (final: null) => void
  onPlaybackState: (isPlaying: boolean) => void
  onVad?: (evt: any) => void
  // kept optional for legacy callers; unused (we removed WAV fallback)
  onSegment?: (audio: Blob) => void
}

export async function connectAndRecord(h: Handlers) {
  // mic capture — starts posting 16k PCM16 frames to our callback
  const { stop: stopMic, onAudio } = await startMic()

  const ws = new WebSocket(WS_URL)
  ws.binaryType = 'arraybuffer'

  // --- ramp guards & state ---
  let vadInSpeech = false
  let lastRampAt = 0
  let playerIsPlaying = false
  const RAMP_COOLDOWN_MS = 600

  // VAD hysteresis counters
  let speechFrames = 0
  let nonSpeechFrames = 0

  const unsubPlayback = onPlaybackState((isPlaying) => {
    playerIsPlaying = isPlaying
    h.onPlaybackState(isPlaying)
    if (DEBUG_VAD) console.log('[AUDIO] playerIsPlaying:', isPlaying)
  })

  function triggerRampDown(reason: string) {
    // only fade if the bot is actually speaking
    if (!playerIsPlaying) {
      logVad('ramp-down skipped: not speaking; reason=', reason)
      return
    }
    const now = performance.now()
    if (now - lastRampAt < RAMP_COOLDOWN_MS) {
      logVad('ramp-down skipped: cooldown; reason=', reason)
      return
    }
    lastRampAt = now
    logVad('ramp-down START; reason=', reason, 'ms=', RAMP_MS)
    playerNode?.port.postMessage({ type: 'ramp_down', ms: RAMP_MS })
  }

  let closed = false

  ws.onopen = async () => {
    try { ws.send(JSON.stringify({ type: 'start' })) } catch {}
    try { await playerCtx?.resume() } catch {}
    if (DEBUG_VAD) console.log('[WS] open → start sent')
  }

  ws.onmessage = (e) => {
    if (typeof e.data === 'string') {
      try {
        const msg = JSON.parse(e.data)
        switch (msg.type) {
          case 'status':
            h.onStatus(msg.message)
            if (DEBUG_VAD) console.log('[WS] status:', msg.message)
            break

          case 'asr_final':
            if (DEBUG_VAD) console.log('[WS] asr_final:', msg.text)
            // FIX: Explicitly clear any lingering audio from the previous turn
            // before handling the new user transcript.
            playerNode?.port.postMessage({ type: 'clear' });
            h.onAsr(msg.text)
            break

          case 'llm_token':
            h.onToken(msg.text)
            break

          case 'turn_done':
            h.onTurnDone()
            break

          case 'done':
            cleanup()
            break

          case 'vad': {
            // e.g. { type:'vad', state:'speech'|'silence'|'noise', prob:0..1, ... }
            h.onVad?.(msg)

            const prob = vadProb(msg)
            const isSpeech = vadIsSpeechLike(msg)

            if (isSpeech) {
              speechFrames++
              nonSpeechFrames = 0
              logVad({ src: 'vad', state: msg.state, prob, speechFrames, playerIsPlaying })
              // IMPORTANT: we DO NOT trigger ramp on raw VAD; too noisy.
              // We only use VAD for UI and to maintain vadInSpeech.
              if (!vadInSpeech && speechFrames >= VAD_MIN_FRAMES) {
                vadInSpeech = true  // rising edge (tracked only)
                h.onStatus('ready')
              }
            } else {
              nonSpeechFrames++
              speechFrames = 0
              logVad({ src: 'vad', state: msg.state, prob, nonSpeechFrames, playerIsPlaying })
              if (vadInSpeech && nonSpeechFrames >= VAD_CLEAR_FRAMES) {
                vadInSpeech = false // falling edge
              }
            }
            break
          }

          case 'utterance': {
            // e.g. { type:'utterance', phase:'begin'|'end', ... }
            h.onVad?.(msg)
            if (msg.phase === 'begin') {
              // Only now do we actually ramp (edge-triggered, not continuous VAD)
              if (!vadInSpeech) {
                vadInSpeech = true
              }
              logVad({ src: 'utterance', phase: 'begin', playerIsPlaying })
              triggerRampDown('utterance-begin')
              h.onStatus('ready')
            } else if (msg.phase === 'end') {
              vadInSpeech = false
              logVad({ src: 'utterance', phase: 'end' })
            }
            break
          }
        }
      } catch {
        // ignore parse errors
      }
      return
    }

    // Binary frames are raw PCM16 LE at 48k. Transfer to the worklet (zero-copy).
    const buf: ArrayBuffer = e.data as ArrayBuffer
    if (playerNode) {
      playerNode.port.postMessage(
        { type: 'push', buffer: buf, byteLength: buf.byteLength },
        [buf] // transfer ownership
      )
    }
  }

  ws.onerror = () => { h.onStatus('error'); cleanup() }
  ws.onclose = cleanup

  const MAX_WS_BUFFER = 2000
  const unsubMic = onAudio((bytes: Uint8Array) => {
    if (ws.readyState !== WebSocket.OPEN) return
    if (ws.bufferedAmount > MAX_WS_BUFFER) return // drop frame; preserve freshness
    ws.send(bytes)
  })

  function cleanup() {
    if (closed) return
    closed = true
    try { ws.close() } catch {}
    try { unsubMic() } catch {}
    try { unsubPlayback() } catch {}
    try { stopMic() } catch {}
    // Clear any queued audio in the worklet
    try { playerNode?.port.postMessage({ type: 'clear' }) } catch {}
    h.onDone(null)
    if (DEBUG_VAD) console.log('[WS] cleanup complete')
  }

  return {
    stop: async () => {
      if (closed) return
      try { ws.send(JSON.stringify({ type: 'stop' })) } catch {}
      // force close if server doesn't respond shortly
      setTimeout(() => { if (!closed) cleanup() }, 5000)
    }
  }
}
