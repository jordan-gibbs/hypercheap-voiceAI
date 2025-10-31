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

        self._in_q: asyncio.Queue[Optional[bytes]] = asyncio.Queue(maxsize=6)
        self._barge_lock = asyncio.Lock()

        self._closed = asyncio.Event()
        self._last_final: Optional[str] = None
        self._pcm_task: Optional[asyncio.Task] = None
        self._speak_task: Optional[asyncio.Task] = None

        self._history: List[Dict[str, str]] = []
        self._hist_lock = asyncio.Lock()
        self._max_history_msgs = 64

        # Outbound streaming callbacks
        self._on_token: Optional[Callable[[str], Awaitable[None]]] = None
        self._on_audio_chunk: Optional[Callable[[bytes], Awaitable[None]]] = None
        self._on_segment_done: Optional[Callable[[], Awaitable[None]]] = None
        self._on_audio_start: Optional[Callable[[], Awaitable[None]]] = None
        self._on_turn_done: Optional[Callable[[], Awaitable[None]]] = None
        self._on_vad: Optional[Callable[[dict], Awaitable[None]]] = None

        self._last_final_ms = 0.0
        self._debounce_ms = 220.0

    async def start(
        self,
        on_asr_final: Optional[Callable[[str], Awaitable[None]]] = None,
        on_token: Optional[Callable[[str], Awaitable[None]]] = None,
        on_audio_chunk: Optional[Callable[[bytes], Awaitable[None]]] = None,
        on_segment_done: Optional[Callable[[], Awaitable[None]]] = None,
        on_audio_start: Optional[Callable[[], Awaitable[None]]] = None,
        on_turn_done: Optional[Callable[[], Awaitable[None]]] = None,
        on_vad: Optional[Callable[[dict], Awaitable[None]]] = None,
    ):
        self._on_token = on_token
        self._on_audio_chunk = on_audio_chunk
        self._on_segment_done = on_segment_done
        self._on_audio_start = on_audio_start
        self._on_turn_done = on_turn_done
        self._on_vad = on_vad

        async def on_vad_inner(evt: dict):
            if self._on_vad:
                with contextlib.suppress(Exception):
                    await self._on_vad(evt)

            t = evt.get("type")
            if (t == "utterance" and evt.get("phase") == "begin") or (t == "vad" and evt.get("state") == "speech"):
                asyncio.create_task(self.barge_in())

        async def on_final(text: str):
            now = time.perf_counter() * 1000.0
            if (now - self._last_final_ms) < self._debounce_ms and text.strip() == (self._last_final or "").strip():
                self._last_final_ms = now
                return
            self._last_final_ms = now

            self._last_final = text

            async with self._hist_lock:
                if not (
                    self._history
                    and self._history[-1].get("role") == "user"
                    and self._history[-1].get("content") == text
                ):
                    self._history.append({"role": "user", "content": text})
                    if len(self._history) > self._max_history_msgs:
                        self._history = self._history[-self._max_history_msgs :]

            if on_asr_final:
                await on_asr_final(text)

            if self._speak_task and not self._speak_task.done():
                logger.info("[session] barge-in detected, interrupting agent.")
                self._speak_task.cancel()

            self._speak_task = asyncio.create_task(self._generate_and_stream(text), name="agent_speak")

        self._pcm_task = asyncio.create_task(self._pump_pcm(), name="agent_pcm")
        await self._fennec.start(on_final=on_final, on_vad=on_vad_inner)

    async def _pump_pcm(self):
        try:
            while not self._closed.is_set():
                chunk = await self._in_q.get()
                if chunk is None:
                    break
                try:
                    await self._fennec.send_pcm(chunk)
                except Exception as e:
                    logger.warning("[session] send_pcm failed: %s", e)
                finally:
                    with contextlib.suppress(ValueError):
                        self._in_q.task_done()
        except asyncio.CancelledError:
            logger.debug("[session] _pump_pcm cancelled")
            raise

    async def feed_pcm(self, pcm_le16: bytes):
        """Enqueue mic PCM without blocking; drop oldest frame if the queue is full."""
        try:
            self._in_q.put_nowait(pcm_le16)
        except asyncio.QueueFull:
            logger.debug("[session] Input queue full. Dropping oldest frame.")
            with contextlib.suppress(asyncio.QueueEmpty):
                _ = self._in_q.get_nowait()
                with contextlib.suppress(ValueError):
                    self._in_q.task_done()
            with contextlib.suppress(Exception):
                self._in_q.put_nowait(pcm_le16)

    _PUNCT = re.compile(r"([.!?…]+|\n)")

    async def _generate_and_stream(self, user_text: str) -> None:
        utext = (user_text or "").strip()
        if not utext:
            return

        async with self._hist_lock:
            full_hist = list(self._history[-self._max_history_msgs :])
        if full_hist and full_hist[-1].get("role") == "user" and full_hist[-1].get("content") == utext:
            hist_for_llm = full_hist[:-1]
        else:
            hist_for_llm = full_hist

        seg_q: asyncio.Queue[Optional[str]] = asyncio.Queue()
        reply_parts: list[str] = []

        async def segment_writer():
            buf: list[str] = []
            char_budget = 250
            t0 = time.perf_counter()
            first_tok_at: Optional[float] = None

            try:
                async for tok in self._llm.stream_reply(utext, history=hist_for_llm):
                    if not tok:
                        continue

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
                logger.debug("[llm] streaming cancelled (barge-in)")
                raise
            finally:
                await seg_q.put(None)

        async def tts_consumer():
            try:
                while True:
                    seg = await seg_q.get()
                    if seg is None:
                        break
                    got_audio = False
                    t1 = time.perf_counter()
                    async for audio in self._tts.synthesize(seg):
                        if not audio:
                            continue
                        if not got_audio:
                            got_audio = True
                            if self._on_audio_start:
                                await self._on_audio_start()
                            logger.info("[latency] tts(first_audio, seg)=%.3fs", time.perf_counter() - t1)
                        if self._on_audio_chunk:
                            await self._on_audio_chunk(audio)
                    if self._on_segment_done:
                        await self._on_segment_done()
            except asyncio.CancelledError:
                logger.debug("[tts] synthesis cancelled (barge-in)")
                # Drain seg_q if cancelled
                while not seg_q.empty():
                    with contextlib.suppress(asyncio.QueueEmpty):
                        _ = seg_q.get_nowait()
                raise

        try:
            await asyncio.gather(segment_writer(), tts_consumer())

            # Only append ASSISTANT if the turn completed successfully
            reply_text = "".join(reply_parts).strip()
            if reply_text:
                async with self._hist_lock:
                    self._history.append({"role": "assistant", "content": reply_text})
                    if len(self._history) > self._max_history_msgs:
                        self._history = self._history[-self._max_history_msgs :]

            if self._on_turn_done:
                await self._on_turn_done()

        except asyncio.CancelledError:
            logger.info("[session] response generation cancelled due to barge-in.")

    async def barge_in(self):
        """Interrupt current LLM/TTS turn immediately when the user starts speaking."""
        if self._barge_lock.locked():
            return
        async with self._barge_lock:
            # Ensure last final is present in history
            async with self._hist_lock:
                if self._last_final and not (
                    self._history
                    and self._history[-1].get("role") == "user"
                    and self._history[-1].get("content") == self._last_final
                ):
                    self._history.append({"role": "user", "content": self._last_final})
                    if len(self._history) > self._max_history_msgs:
                        self._history = self._history[-self._max_history_msgs :]

            # Cancel any active speak task
            if self._speak_task and not self._speak_task.done():
                self._speak_task.cancel()
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await asyncio.wait_for(self._speak_task, timeout=0.5)

            # Try to stop TTS stream immediately (if supported)
            with contextlib.suppress(Exception):
                stop_fn = getattr(self._tts, "stop", None)
                if callable(stop_fn):
                    maybe = stop_fn()
                    if asyncio.iscoroutine(maybe):
                        await maybe

            # Optionally notify the LLM implementation (if it supports cancellation)
            with contextlib.suppress(Exception):
                cancel_fn = getattr(self._llm, "cancel", None)
                if callable(cancel_fn):
                    maybe = cancel_fn()
                    if asyncio.iscoroutine(maybe):
                        await maybe

    async def flush_and_reply_audio(self) -> AsyncIterator[bytes]:
        if False:
            yield b""  # pragma: no cover

    async def stop(self):
        # Stop the PCM pump
        if self._pcm_task and not self._pcm_task.done():
            with contextlib.suppress(Exception):
                self._in_q.put_nowait(None)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._pcm_task, timeout=2.0)

        # Allow any active TTS turn to finish quickly
        if self._speak_task and not self._speak_task.done():
            self._speak_task.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(self._speak_task, timeout=5.0)

    async def close(self):
        await self.stop()
        self._closed.set()
        await self._fennec.stop()
        with contextlib.suppress(Exception):
            await self._tts.close()
