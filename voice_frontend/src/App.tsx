import React, { useEffect, useRef, useState } from 'react'
import { connectAndRecord, primePlayer } from './lib/ws'

type ChatItem = { role: 'user' | 'assistant', content: string }
// Define precise UI states for accurate labeling
type UIStatus = 'idle' | 'connecting' | 'initializing' | 'ready' | 'speaking' | 'thinking' | 'error' | 'stopping';

function useTheme() {
  const init = (): 'light' | 'dark' => {
    const saved = localStorage.getItem('theme') as 'light' | 'dark' | null
    if (saved) return saved
    return matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
  }
  const [theme, setTheme] = useState<'light'|'dark'>(init)
  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme)
    localStorage.setItem('theme', theme)
  }, [theme])
  return { theme, toggle: () => setTheme(t => t === 'dark' ? 'light' : 'dark') }
}

const Sun = () => (
  <svg className="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
    <circle cx="12" cy="12" r="4" />
    <path d="M12 2v2m0 16v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2m16 0h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/>
  </svg>
)
const Moon = () => (
  <svg className="icon" viewBox="0 0 24 24" fill="currentColor">
    <path d="M21 12.79A9 9 0 1 1 11.21 3a7 7 0 1 0 9.79 9.79Z"/>
  </svg>
)

export default function App() {
  const { theme, toggle } = useTheme()

  const [status, setStatus] = useState<UIStatus>('idle')
  const [active, setActive] = useState(false) // True if session is ongoing
  const [chat, setChat] = useState<ChatItem[]>([])
  const [assistantDraft, setAssistantDraft] = useState('')

  // State for accurate labeling
  const [isThinking, setIsThinking] = useState(false); // Tracks if LLM is active

  const assistantDraftRef = useRef(assistantDraft)
  useEffect(() => { assistantDraftRef.current = assistantDraft }, [assistantDraft])

  // Playback mechanism (supports fallback WAV stitching if AudioWorklet fails)
  const audioRef = useRef<HTMLAudioElement>(null)
  const queueRef = useRef<Blob[]>([])
  const playingRef = useRef(false) // Tracks fallback playback state
  const workletPlayingRef = useRef(false) // Tracks worklet playback state

  const wsRef = useRef<Awaited<ReturnType<typeof connectAndRecord>> | null>(null);

  // Auto-scroll transcript
  const transcriptRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const el = transcriptRef.current
    if (!el) return
    el.scrollTop = el.scrollHeight
  }, [chat, assistantDraft])

  // Helper function to stop fallback playback (Worklet is stopped via ws.ts events)
  const stopFallbackPlayback = () => {
    playingRef.current = false;
    queueRef.current = [];
    if (audioRef.current) {
        audioRef.current.pause();
        if (audioRef.current.src) URL.revokeObjectURL(audioRef.current.src);
        audioRef.current.src = '';
    }
  }

  // Fallback playback logic
  const playNext = async () => {
    if (!audioRef.current) return
    if (playingRef.current) return
    const next = queueRef.current.shift()
    if (!next) {
      // Fallback queue empty. Status update handled by the derived status useEffect.
      return
    }
    playingRef.current = true;
    audioRef.current.src = URL.createObjectURL(next)
    try { await audioRef.current.play() } catch {}
  }

  useEffect(() => {
    const a = audioRef.current
    if (!a) return
    const onEnded = () => {
      playingRef.current = false
      void playNext()
    }
    a.addEventListener('ended', onEnded)
    return () => a.removeEventListener('ended', onEnded)
  }, [])

  // Derive the UI status from the underlying states (isThinking, isPlaying, active)
  useEffect(() => {
    const isPlaying = workletPlayingRef.current || playingRef.current;

    // Handle transitional states explicitly set during start/stop flow
    if (['idle', 'connecting', 'initializing', 'stopping', 'error'].includes(status)) {
        return;
    }

    // Determine dynamic state during an active session
    if (active) {
        if (isPlaying) {
            setStatus('speaking');
        } else if (isThinking) {
            setStatus('thinking');
        } else {
            // Active, not thinking, not speaking -> Ready/Listening
            setStatus('ready');
        }
    } else if (status !== 'idle' && status !== 'error') {
        // If not active and not already idle/error, transition to idle
        setStatus('idle');
    }
    // We use JSON.stringify(workletPlayingRef.current) and playingRef.current as dependencies
    // because the refs themselves don't trigger updates, but their .current value changing should.
  }, [isThinking, JSON.stringify(workletPlayingRef.current), JSON.stringify(playingRef.current), active, status]);


  async function start() {
    // Create/resume the playback AudioContext in response to this click
    try {
      await primePlayer()
    } catch (e) {
      console.error("Failed to prime audio player:", e);
      setStatus('error');
      return;
    }

    // Reset UI and connect streams
    setChat([])
    setAssistantDraft('')
    setIsThinking(false);
    setStatus('connecting') // Initial connection phase

    const onAsr = (t: string) => {
      // User spoke. System starts thinking.
      setIsThinking(true);

      // BARGE-IN: Stop any ongoing fallback playback immediately.
      // ws.ts handles clearing the worklet buffer when asr_final is received.
      stopFallbackPlayback();

      // A new user message means the previous AI turn is complete (or interrupted).
      // Finalize the last assistant message and add the new user message.
      setChat(prev => {
        const newChat = [...prev]
        // 1. If there's an assistant draft, commit it to the chat.
        if (assistantDraftRef.current) {
          newChat.push({ role: 'assistant', content: assistantDraftRef.current })
        }
        // 2. Add the new user message.
        newChat.push({ role: 'user', content: t })
        return newChat
      })
      // 3. Clear the draft for the next AI response.
      setAssistantDraft('')
    }

    const onStatus = (s: string) => {
      // Handle synchronization statuses from backend
      if (s === 'connected') setStatus('connecting');
      else if (s === 'initializing') setStatus('initializing');
      else if (s === 'ready') setStatus('ready');
      else if (s === 'error') setStatus('error');
    }

    const onToken = (tok: string) => {
      // We are receiving tokens, so we are definitely thinking.
      setIsThinking(true);
      setAssistantDraft(prev => prev + tok)
    }

    const onSegment = (blob: Blob) => {
      // A segment of audio is ready.
      // If using fallback (blob size > 0), queue it.
      if (blob && blob.size > 0) {
        console.warn("Using fallback audio element playback.");
        queueRef.current.push(blob)
        void playNext()
      }
      // If using streaming (blob size == 0), the worklet handles playback and state updates via onPlaybackState.
    }

    const onTurnDone = () => {
        // LLM generation is complete for this turn.
        setIsThinking(false);
    }

    const onPlaybackState = (isPlaying: boolean) => {
        workletPlayingRef.current = isPlaying;
        // Trigger re-evaluation of the derived status by toggling a dependency of the useEffect
        setIsThinking(prev => prev);
    }

    const onDone = () => {
      // Session is over. Commit the very last assistant message if it exists.
      setChat(prev => {
        if (assistantDraftRef.current) {
          return [...prev, { role: 'assistant', content: assistantDraftRef.current }]
        }
        return prev
      })
      setAssistantDraft('') // clear ephemeral draft

      // Tear down UI/connection state
      stopFallbackPlayback();
      setIsThinking(false);

      // Use a temporary check for the status *before* onDone was called
      const currentStatus = document.documentElement.getAttribute('data-status') || status;

      // If status was error, keep it, otherwise set to idle.
      if (currentStatus !== 'error') {
        setStatus('idle');
      }
      wsRef.current = null
      setActive(false)
    }

    assistantDraftRef.current = ''
    try {
      wsRef.current = await connectAndRecord({ onAsr, onStatus, onToken, onSegment, onDone, onPlaybackState, onTurnDone })
      setActive(true)
    } catch (e) {
      console.error("Failed to connect or record:", e);
      setStatus('error');
      setActive(false);
    }
  }

  async function stop() {
    if (!wsRef.current) return
    setStatus('stopping') // Indicate closing down
    await wsRef.current.stop()
    // onDone will handle the final transition
  }

  async function toggleMic() {
    // Prevent toggling if in a transitional or error state
    if (['connecting', 'initializing', 'stopping', 'error'].includes(status)) {
        return;
    }
    if (active) await stop()
    else await start()
  }

  // Updated labels to match the actual activity
  const badgeText = (() => {
    switch (status) {
      case 'idle': return 'Idle';
      case 'connecting': return 'Connecting…'; // WS connection
      case 'initializing': return 'Initializing…'; // Waiting for Fennec/VAD
      case 'ready': return 'Listening…'; // Actively listening (VAD on)
      case 'thinking': return 'Thinking…'; // Waiting for LLM
      case 'speaking': return 'Speaking…'; // AI is talking
      case 'stopping': return 'Stopping…';
      case 'error': return 'Error';
      default: return 'Waiting…';
    }
  })();

  // Store status in DOM attribute for access in onDone if needed (slight hack for cleanup logic)
  useEffect(() => {
    document.documentElement.setAttribute('data-status', status);
  }, [status]);

  return (
    <div className="container">
      <header className="header">
        <div className="brand">
          <div className="logo" />
          <div>
            <div className="title">Hyper-Cheap Voice Agent</div>
            <div className="caption">Fennec ASR → Baseten LLaMa → Inworld TTS</div>
          </div>
        </div>
        <button className="icon-btn" onClick={toggle} aria-label="Toggle theme">
          {theme === 'dark' ? <Sun/> : <Moon/>}
        </button>
      </header>

      <section className="hero">
        <div className="card">
          <div className="controls">
            <button
              className={['mic', active ? 'active' : ''].join(' ')}
              onClick={toggleMic}
              aria-pressed={active}
              title={active ? 'Click to stop' : 'Click to start'}
              disabled={['connecting', 'initializing', 'stopping', 'error'].includes(status)}
            >
              🎤
            </button>
            <div className="badge">{badgeText}</div>
          </div>
          <div className="caption" style={{marginTop: 10}}>
            Click once to start, converse freely (interrupts supported); click again to end.
          </div>
        </div>

        <div className="card transcript" ref={transcriptRef}>
          {chat.length === 0 && !assistantDraft ? (
            <span className="caption">Transcript will appear here…</span>
          ) : (
            <div style={{display:'grid', gap: '10px'}}>
              {chat.map((m, i) => (
                <div key={i} style={{
                  alignSelf: m.role === 'user' ? 'start' : 'end',
                  background: 'color-mix(in lab, var(--card), transparent 0%)',
                  border: '1px solid color-mix(in lab, var(--ring), transparent 80%)',
                  borderRadius: 12,
                  padding: '10px 12px',
                  maxWidth: '85%',
                }}>
                  <div className="caption" style={{marginBottom: 4}}>{m.role}</div>
                  <div>{m.content}</div>
                </div>
              ))}
              {assistantDraft && (
                <div style={{
                  alignSelf: 'end',
                  background: 'color-mix(in lab, var(--card), transparent 0%)',
                  border: '1px solid color-mix(in lab, var(--ring), transparent 70%)',
                  borderRadius: 12,
                  padding: '10px 12px',
                  maxWidth: '85%',
                  opacity: 0.9
                }}>
                  <div className="caption" style={{marginBottom: 4}}>assistant</div>
                  <div>{assistantDraft}</div>
                </div>
              )}
            </div>
          )}
        </div>

        <audio ref={audioRef} />
      </section>
    </div>
  )
}