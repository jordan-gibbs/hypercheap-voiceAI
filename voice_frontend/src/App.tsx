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

  const [isThinking, setIsThinking] = useState(false); // Tracks if LLM is active

  const assistantDraftRef = useRef(assistantDraft)
  useEffect(() => { assistantDraftRef.current = assistantDraft }, [assistantDraft])

  // Playback mechanism (supports fallback WAV stitching if AudioWorklet fails)
  const audioRef = useRef<HTMLAudioElement>(null)
  const queueRef = useRef<Blob[]>([])
  const playingRef = useRef(false) // Tracks fallback playback state
  const workletPlayingRef = useRef(false) // Tracks worklet playback state

  const wsRef = useRef<Awaited<ReturnType<typeof connectAndRecord>> | null>(null);

  const transcriptRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const el = transcriptRef.current
    if (!el) return
    el.scrollTop = el.scrollHeight
  }, [chat, assistantDraft])

  const stopFallbackPlayback = () => {
    playingRef.current = false;
    queueRef.current = [];
    if (audioRef.current) {
        audioRef.current.pause();
        if (audioRef.current.src) URL.revokeObjectURL(audioRef.current.src);
        audioRef.current.src = '';
    }
  }

  const playNext = async () => {
    if (!audioRef.current) return
    if (playingRef.current) return
    const next = queueRef.current.shift()
    if (!next) {
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

  useEffect(() => {
    const isPlaying = workletPlayingRef.current || playingRef.current;

    if (['idle', 'connecting', 'initializing', 'stopping', 'error'].includes(status)) {
        return;
    }

    if (active) {
        if (isPlaying) {
            setStatus('speaking');
        } else if (isThinking) {
            setStatus('thinking');
        } else {
            setStatus('ready');
        }
    } else if (status !== 'idle' && status !== 'error') {
        setStatus('idle');
    }
  }, [isThinking, JSON.stringify(workletPlayingRef.current), JSON.stringify(playingRef.current), active, status]);


  async function start() {
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
    setStatus('connecting')

    const onAsr = (t: string) => {

      setIsThinking(true);

      stopFallbackPlayback();

      setChat(prev => {
        const newChat = [...prev]
        if (assistantDraftRef.current) {
          newChat.push({ role: 'assistant', content: assistantDraftRef.current })
        }
        newChat.push({ role: 'user', content: t })
        return newChat
      })
      setAssistantDraft('')
    }

    const onStatus = (s: string) => {
      if (s === 'connected') setStatus('connecting');
      else if (s === 'initializing') setStatus('initializing');
      else if (s === 'ready') setStatus('ready');
      else if (s === 'error') setStatus('error');
    }

    const onToken = (tok: string) => {
      setIsThinking(true);
      setAssistantDraft(prev => prev + tok)
    }

    const onSegment = (blob: Blob) => {
      if (blob && blob.size > 0) {
        console.warn("Using fallback audio element playback.");
        queueRef.current.push(blob)
        void playNext()
      }
    }

    const onTurnDone = () => {
        setIsThinking(false);
    }

    const onPlaybackState = (isPlaying: boolean) => {
        workletPlayingRef.current = isPlaying;
        setIsThinking(prev => prev);
    }

    const onDone = () => {
      setChat(prev => {
        if (assistantDraftRef.current) {
          return [...prev, { role: 'assistant', content: assistantDraftRef.current }]
        }
        return prev
      })
      setAssistantDraft('')

      stopFallbackPlayback();
      setIsThinking(false);

      const currentStatus = document.documentElement.getAttribute('data-status') || status;

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
    setStatus('stopping')
    await wsRef.current.stop()
  }

  async function toggleMic() {
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

  useEffect(() => {
    document.documentElement.setAttribute('data-status', status);
  }, [status]);

  return (
    <div className="container">
      <header className="header">
        <div className="brand">
          <div>
            <div className="title">Hyper-Cheap Voice Agent</div>
            <div className="caption">Fennec ASR → Baseten Qwen → Inworld TTS</div>
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