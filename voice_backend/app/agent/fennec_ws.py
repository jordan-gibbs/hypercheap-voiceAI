import asyncio
import json
import logging
from typing import Awaitable, Callable, Optional
import contextlib

import websockets

logger = logging.getLogger("hypercheap.fennec")

DEFAULT_VAD = {
    "threshold": 0.40,
    "min_silence_ms": 200,
    "speech_pad_ms": 240,
    "final_silence_s": 0.20,
    "start_trigger_ms": 24,
    "min_voiced_ms": 36,
    "min_chars": 1,
    "min_words": 1,
    "amp_extend": 1200,
    "force_decode_ms": 0,
}

_FINAL_KEYS = {"final", "is_final"}
_FINAL_TYPES = {"final", "transcript_final", "eos"}


class FennecWSClient:

    def __init__(
        self,
        api_key: str,
        sample_rate: int = 16000,
        channels: int = 1,
        vad: Optional[dict] = None,
        callback_timeout_s: float = 5.0,
        max_pending_final: int = 64,
        ping_interval: float = 5.0,
        ping_timeout: float = 5.0,
        open_timeout: float = 30.0,
    ) -> None:
        self._api_key = api_key
        self._sr = sample_rate
        self._ch = channels
        self._vad = vad or DEFAULT_VAD

        self._url = (
            f"wss://api.fennec-asr.com/api/v1/transcribe/stream?api_key={self._api_key}"
        )

        self._ws: Optional[websockets.WebSocketClientProtocol] = None

        self._send_q: asyncio.Queue[Optional[bytes]] = asyncio.Queue(maxsize=256)
        self._final_q: asyncio.Queue[Optional[str]] = asyncio.Queue(
            maxsize=max_pending_final
        )

        self._send_task: Optional[asyncio.Task] = None
        self._recv_task: Optional[asyncio.Task] = None
        self._final_task: Optional[asyncio.Task] = None

        self._on_final: Optional[Callable[[str], Awaitable[None]]] = None
        self._on_partial: Optional[Callable[[str], Awaitable[None]]] = None  # optional

        self._started = asyncio.Event()
        self._stopped = False

        self._callback_timeout_s = callback_timeout_s
        self._ping_interval = ping_interval
        self._ping_timeout = ping_timeout
        self._open_timeout = open_timeout

    async def start(
        self,
        on_final: Callable[[str], Awaitable[None]],
        on_partial: Optional[Callable[[str], Awaitable[None]]] = None,
    ):
        if self._ws is not None:
            return

        self._stopped = False
        self._on_final = on_final
        self._on_partial = on_partial

        logger.info("[fennec] connect %s", self._url)
        self._ws = await websockets.connect(
            self._url,
            max_size=None,
            ping_interval=self._ping_interval,
            ping_timeout=self._ping_timeout,
            open_timeout=self._open_timeout,
        )

        start_msg = {
            "type": "start",
            "sample_rate": self._sr,
            "channels": self._ch,
            "single_utterance": False,
            "vad": self._vad,
            "format": "pcm_s16le",
        }
        await self._ws.send(json.dumps(start_msg))
        self._started.set()
        logger.info("[fennec] started; sent VAD config")

        self._send_task = asyncio.create_task(self._send_loop(), name="fennec_send")
        self._recv_task = asyncio.create_task(self._recv_loop(), name="fennec_recv")
        self._final_task = asyncio.create_task(
            self._final_dispatch_loop(), name="fennec_final"
        )

    async def send_pcm(self, pcm_le16: bytes) -> None:
        await self._started.wait()
        if self._ws is None or self._stopped:
            return
        try:
            await self._send_q.put(pcm_le16)
        except asyncio.QueueFull:
            # Drop oldest to keep latency bounded
            _ = self._send_q.get_nowait()
            self._send_q.task_done()
            await self._send_q.put(pcm_le16)

    async def _send_loop(self):
        try:
            while True:
                item = await self._send_q.get()
                if item is None:
                    break
                try:
                    await self._ws.send(item)
                except Exception as e:
                    logger.warning("[fennec] send error: %s", e)
                    break
        finally:
            try:
                if self._ws and self._ws.open:
                    await self._ws.send('{"type":"eos"}')
            except Exception:
                pass

    async def _recv_loop(self):
        try:
            async for msg in self._ws:
                if isinstance(msg, (bytes, bytearray)):
                    # (reserved for future server features)
                    continue

                data = None
                try:
                    data = json.loads(msg)
                except Exception:
                    continue

                msg_type = data.get("type")
                text = (data.get("text") or "").strip()
                is_final_flag = any(data.get(k) is True for k in _FINAL_KEYS)
                is_final = is_final_flag or (msg_type in _FINAL_TYPES)

                if is_final and text:
                    try:
                        self._final_q.put_nowait(text)
                    except asyncio.QueueFull:
                        _ = self._final_q.get_nowait()
                        self._final_q.task_done()
                        self._final_q.put_nowait(text)
                elif text and self._on_partial:
                    asyncio.create_task(self._safe_call_partial(text))
        except websockets.exceptions.ConnectionClosed as e:
            logger.info("[fennec] connection closed by server (code=%s)", getattr(e, "code", "?"))
        except Exception as e:
            logger.warning("[fennec] recv error: %s", e)

    async def _final_dispatch_loop(self):
        while True:
            text = await self._final_q.get()
            if text is None:
                break
            try:
                if self._on_final:
                    await asyncio.wait_for(
                        self._on_final(text), timeout=self._callback_timeout_s
                    )
            except asyncio.TimeoutError:
                logger.warning("[fennec] on_final timed out after %.1fs", self._callback_timeout_s)
            except Exception:
                logger.exception("[fennec] on_final raised")
            finally:
                self._final_q.task_done()

    async def _safe_call_partial(self, text: str):
        try:
            await asyncio.wait_for(
                self._on_partial(text), timeout=min(1.0, self._callback_timeout_s / 5)
            )
        except Exception:
            pass

    async def stop(self):
        if self._stopped:
            return
        self._stopped = True

        with contextlib.suppress(Exception):
            await self._send_q.put(None)

        with contextlib.suppress(Exception):
            if self._ws and self._ws.open:
                await self._ws.close()

        with contextlib.suppress(Exception):
            await self._final_q.put(None)

        for t, name in [
            (self._send_task, "send"),
            (self._recv_task, "recv"),
            (self._final_task, "final"),
        ]:
            if not t:
                continue
            try:
                await asyncio.wait_for(t, timeout=2.0)
            except Exception:
                t.cancel()

        self._ws = None
        self._send_task = None
        self._recv_task = None
        self._final_task = None
        self._on_final = None
        self._on_partial = None
        self._started = asyncio.Event()

        logger.info("[fennec] stopped")

    async def close(self):
        await self.stop()
