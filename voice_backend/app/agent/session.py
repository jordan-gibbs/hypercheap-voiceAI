import asyncio
import contextlib
import logging
import re
import time
from typing import AsyncIterator, Awaitable, Callable, Dict, List, Optional

from .fennec_ws import FennecWSClient
from .inworld_tts import InworldTTS
from .llm_client import BasetenChat

logger = logging.getLogger("hypercheap.session")


class AgentSession:
    def __init__(self, fennec: FennecWSClient, llm: BasetenChat, tts: InworldTTS) -> None:
        self._fennec = fennec
        self._llm = llm
        self._tts = tts

        self._in_q: asyncio.Queue[bytes] = asyncio.Queue()
        self._closed = asyncio.Event()
        self._last_final: Optional[str] = None
        self._pcm_task: Optional[asyncio.Task] = None
        self._speak_task: Optional[asyncio.Task] = None

        # Rolling chat history (server-side memory)
        self._history: List[Dict[str, str]] = []  # [{"role":"user"|"assistant","content":"..."}]
        self._hist_lock = asyncio.Lock()
        self._max_history_msgs = 16  # keep last ~8 turns

        # Outbound streaming callbacks
        self._on_token: Optional[Callable[[str], Awaitable[None]]] = None
        self._on_audio_chunk: Optional[Callable[[bytes], Awaitable[None]]] = None
        self._on_segment_done: Optional[Callable[[], Awaitable[None]]] = None
        self._on_audio_start: Optional[Callable[[], Awaitable[None]]] = None
        self._on_turn_done: Optional[Callable[[], Awaitable[None]]] = None  # NEW

    async def start(
        self,
        on_asr_final: Optional[Callable[[str], Awaitable[None]]] = None,
        on_token: Optional[Callable[[str], Awaitable[None]]] = None,
        on_audio_chunk: Optional[Callable[[bytes], Awaitable[None]]] = None,
        on_segment_done: Optional[Callable[[], Awaitable[None]]] = None,
        on_audio_start: Optional[Callable[[], Awaitable[None]]] = None,
        on_turn_done: Optional[Callable[[], Awaitable[None]]] = None,  # NEW
    ):
        self._on_token = on_token
        self._on_audio_chunk = on_audio_chunk
        self._on_segment_done = on_segment_done
        self._on_audio_start = on_audio_start
        self._on_turn_done = on_turn_done  # NEW

        async def on_final(text: str):
            self._last_final = text

            # 1. BARGE-IN: Stop ongoing speech if any.
            if self._speak_task and not self._speak_task.done():
                logger.info("[session] barge-in detected, interrupting agent.")
                self._speak_task.cancel()
                # Wait for the task to actually stop and handle the CancelledError.
                try:
                    await self._speak_task
                except asyncio.CancelledError:
                    logger.info("[session] previous turn successfully interrupted.")
                except Exception as e:
                    logger.error("[session] error during interruption: %s", e)

            # 2. Notify frontend about the user transcript.
            # The frontend uses this event (asr_final) to clear its audio buffer immediately.
            if on_asr_final:
                await on_asr_final(text)

            # 3. Start the new response generation immediately.
            self._speak_task = asyncio.create_task(self._generate_and_stream(text))

        # This awaits Fennec connection and VAD config send.
        await self._fennec.start(on_final=on_final)
        self._pcm_task = asyncio.create_task(self._pump_pcm())

    async def _pump_pcm(self):
        while not self._closed.is_set():
            chunk = await self._in_q.get()
            if chunk is None:
                break
            await self._fennec.send_pcm(chunk)

    async def feed_pcm(self, pcm_le16: bytes):
        await self._in_q.put(pcm_le16)

    # Stream TTS per sentence-sized segment while the LLM is still streaming
    _PUNCT = re.compile(r"([.!?…]+|\n)")

    async def _generate_and_stream(self, user_text: str) -> None:
        """LLM (with history) → segment tokens → stream TTS audio per segment → notify per segment."""
        utext = (user_text or "").strip()
        if not utext:
            return

        # Snapshot history (thread-safe)
        async with self._hist_lock:
            hist = list(self._history[-self._max_history_msgs :])

        seg_q: asyncio.Queue[Optional[str]] = asyncio.Queue()
        reply_parts: list[str] = []

        async def segment_writer():
            # Collect tokens and cut at punctuation/length for earlier audio
            buf: list[str] = []
            char_budget = 250
            t0 = time.perf_counter()
            first_tok_at: Optional[float] = None

            try:
                # This stream (AsyncOpenAI based) is cancellable if the parent task is cancelled.
                async for tok in self._llm.stream_reply(utext, history=hist):
                    if tok:
                        reply_parts.append(tok)
                        if self._on_token:
                            await self._on_token(tok)
                        if first_tok_at is None:
                            first_tok_at = time.perf_counter()
                            logger.info("[latency] llm first_token=%.3fs", first_tok_at - t0)

                        buf.append(tok)
                        s = "".join(buf)
                        if len(s) >= char_budget or self._PUNCT.search(s):
                            await seg_q.put(s.strip())
                            buf.clear()

                tail = "".join(buf).strip()
                if tail:
                    await seg_q.put(tail)
            except asyncio.CancelledError:
                logger.debug("[llm] streaming cancelled")
                raise  # Propagate cancellation to asyncio.gather
            finally:
                # Ensure TTS consumer always terminates, even if cancelled.
                await seg_q.put(None)

        async def tts_consumer():
            try:
                while True:
                    seg = await seg_q.get()
                    if seg is None:
                        break
                    got_audio = False
                    t1 = time.perf_counter()
                    # This stream (httpx based) is cancellable if the parent task is cancelled.
                    async for audio in self._tts.synthesize(seg):
                        if audio:
                            # Fire audio_start *once per segment* on first bytes
                            if not got_audio:
                                got_audio = True
                                if self._on_audio_start:
                                    await self._on_audio_start()
                                logger.info(
                                    "[latency] tts(first_audio, seg)=%.3fs",
                                    time.perf_counter() - t1,
                                )
                            if self._on_audio_chunk:
                                await self._on_audio_chunk(audio)
                    if self._on_segment_done:
                        await self._on_segment_done()
            except asyncio.CancelledError:
                logger.debug("[tts] synthesis cancelled")
                raise  # Propagate cancellation to asyncio.gather

        try:
            # asyncio.gather propagates cancellation to children (writer and consumer).
            await asyncio.gather(segment_writer(), tts_consumer())

            # --- SUCCESSFUL COMPLETION ---
            # Finalize one assistant turn in history (only runs if not cancelled)
            reply_text = "".join(reply_parts).strip()
            if reply_text:
                async with self._hist_lock:
                    self._history.append({"role": "user", "content": utext})
                    self._history.append({"role": "assistant", "content": reply_text})
                    if len(self._history) > self._max_history_msgs:
                        self._history = self._history[-self._max_history_msgs :]

            # Signal that the LLM generation is complete for this turn.
            if self._on_turn_done:
                await self._on_turn_done()

        except asyncio.CancelledError:
            # --- INTERRUPTED ---
            # This block executes when barge-in occurs.
            # We do not save the partial history.
            logger.info("[session] response generation cancelled due to barge-in.")
            raise  # Propagate to the caller (on_final) so it knows the task was cancelled.

    async def flush_and_reply_audio(self) -> AsyncIterator[bytes]:
        """Kept for compatibility (not used in auto-stream path)."""
        # Implementation omitted for brevity as it is unused in the main flow,
        # but it should also be updated to handle cancellation if used.
        # See previous candidates for a potential implementation if needed.
        pass

    async def stop(self):
        if self._pcm_task and not self._pcm_task.done():
            self._in_q.put_nowait(None)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._pcm_task, timeout=2.0)
        if self._speak_task and not self._speak_task.done():
            with contextlib.suppress(Exception):
                # Wait for the current turn to finish gracefully (this is stop, not interrupt)
                await asyncio.wait_for(self._speak_task, timeout=5.0)

    async def close(self):
        await self.stop()
        self._closed.set()
        await self._fennec.stop()
        with contextlib.suppress(Exception):
            await self._tts.close()
