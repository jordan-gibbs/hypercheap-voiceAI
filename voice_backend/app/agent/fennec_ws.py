import asyncio
import json
import logging
from typing import Awaitable, Callable, Optional

import websockets

logger = logging.getLogger("hypercheap.fennec")

# Aggressive VAD for rapid performance
DEFAULT_VAD = {
    "threshold": 0.35,
    "min_silence_ms": 80,
    "speech_pad_ms": 240,
    "final_silence_s": 0.05,
    "start_trigger_ms": 24,
    "min_voiced_ms": 36,
    "min_chars": 1,
    "min_words": 1,
    "amp_extend": 1200,
    "force_decode_ms": 0,
}


class FennecWSClient:
    """
    Minimal WebSocket client for Fennec ASR streaming:
      - send raw PCM16 LE @ 16kHz as binary frames
      - receive JSON text frames with final transcripts: {"text": "..."}
    Robust stop(): sends EOS, closes the socket, and joins tasks with timeouts
    to avoid hanging the first session and blocking the next.
    """

    def __init__(
        self, api_key: str, sample_rate: int = 16000, channels: int = 1, vad: dict | None = None
    ) -> None:
        self._api_key = api_key
        self._sr = sample_rate
        self._ch = channels
        self._vad = vad or DEFAULT_VAD

        self._url = f"wss://api.fennec-asr.com/api/v1/transcribe/stream?api_key={self._api_key}"

        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._send_q: asyncio.Queue[Optional[bytes]] = asyncio.Queue()

        self._send_task: Optional[asyncio.Task] = None
        self._recv_task: Optional[asyncio.Task] = None

        self._on_final: Optional[Callable[[str], Awaitable[None]]] = None
        self._started = asyncio.Event()
        self._stopped = False

    async def start(self, on_final: Callable[[str], Awaitable[None]]):
        if self._ws is not None:
            return
        self._on_final = on_final

        logger.info("[fennec] connect %s", self._url)
        self._ws = await websockets.connect(
            self._url,
            max_size=None,
            ping_interval=10,
            ping_timeout=10,
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

    async def send_pcm(self, pcm_le16: bytes) -> None:
        await self._started.wait()
        if self._ws is None:
            return
        await self._send_q.put(pcm_le16)

    async def _send_loop(self):
        try:
            while True:
                item = await self._send_q.get()
                if item is None:
                    break
                try:
                    await self._ws.send(item)  # binary frame
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
                    continue
                try:
                    data = json.loads(msg)
                except Exception:
                    continue
                if "text" in data and data["text"]:
                    logger.info("[fennec] final: %s", data["text"])
                    if self._on_final:
                        await self._on_final(data["text"])
        except websockets.exceptions.ConnectionClosed:
            logger.info("[fennec] connection closed by server")
        except Exception as e:
            logger.warning("[fennec] recv error: %s", e)

    async def stop(self):
        if self._stopped:
            return
        self._stopped = True

        try:
            await self._send_q.put(None)
            if self._send_task:
                await asyncio.wait_for(self._send_task, timeout=2.0)
        except Exception:
            if self._send_task:
                self._send_task.cancel()

        try:
            if self._ws and self._ws.open:
                await self._ws.close()
        except Exception:
            pass

        try:
            if self._recv_task:
                await asyncio.wait_for(self._recv_task, timeout=2.0)
        except Exception:
            if self._recv_task:
                self._recv_task.cancel()

        self._ws = None
        self._send_task = None
        self._recv_task = None
        self._on_final = None
        logger.info("[fennec] stopped")
