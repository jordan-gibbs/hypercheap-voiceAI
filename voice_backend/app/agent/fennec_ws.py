# agent/fennec_ws.py

import asyncio
import json
import logging
from typing import Awaitable, Callable, Literal, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger("hypercheap.fennec")

DEFAULT_VAD = {
    "threshold": 0.35,
    "min_silence_ms": 50,
    "speech_pad_ms": 350,
    "final_silence_s": 0.05,
    "start_trigger_ms": 24,
    "min_voiced_ms": 36,
    "min_chars": 1,
    "min_words": 1,
    "amp_extend": 600,
    "force_decode_ms": 0,
}

Eagerness = Literal["low", "medium", "high"]


class FennecWSClient:
    """
    Minimal WebSocket client for Fennec ASR.

    Auth flow (per Fennec docs):
      1) POST your API key to /api/v1/transcribe/streaming-token to get a short-lived JWT.
      2) Connect to wss://api.fennec-asr.com/api/v1/transcribe/stream?streaming_token=<JWT>
         (No X-API-Key header on the WebSocket.)
    Thought detection is optional and OFF by default. When ON, the server returns
    'complete_thought' messages (and may also emit 'corrected_transcript').
    Public API: start(), send_pcm(), stop(), close()
    """

    def __init__(
        self,
        api_key: str,
        sample_rate: int = 16000,
        channels: int = 1,
        vad: Optional[dict] = None,
        *,
        # Thought-detection options (mirror your React)
        detect_thoughts: bool = False,
        end_thought_eagerness: Eagerness = "high",
        force_complete_time: float = 20,  # seconds
        context: Optional[str] = None,
        ping_interval: float = 5.0,
        url: str = "wss://api.fennec-asr.com/api/v1/transcribe/stream",
        token_service_url: str = "https://api.fennec-asr.com/api/v1/transcribe/streaming-token",
    ) -> None:
        self._api_key = api_key
        self._sr = sample_rate
        self._ch = channels
        self._vad = vad or DEFAULT_VAD

        self._detect_thoughts = detect_thoughts
        self._end_thought_eagerness = end_thought_eagerness
        self._force_complete_time = force_complete_time
        self._context = context

        self._url = url
        self._ping_interval = ping_interval
        self._token_service_url = token_service_url

        self._ws = None
        self._recv_task: Optional[asyncio.Task] = None
        self._on_final: Optional[Callable[[str], Awaitable[None]]] = None
        self._on_vad: Optional[Callable[[dict], Awaitable[None]]] = None
        self._ready = asyncio.Event()  # set after server 'ready'
        self._closed = False

    async def _fetch_streaming_token(self) -> str:
        """
        Exchange API key for a short-lived streaming token.
        Sends API key via X-API-Key to token service; expects {"token": "<JWT>"}.
        """
        if not self._api_key:
            raise RuntimeError("Fennec API key is required to obtain a streaming token.")
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    self._token_service_url,
                    headers={"X-API-Key": self._api_key, "content-type": "application/json"},
                    json={},  # body not required
                )
                resp.raise_for_status()
                data = resp.json()
                token = data.get("token")
                if not token:
                    raise RuntimeError(f"Token endpoint returned no token: {data}")
                return token
        except Exception as e:
            logger.error("[fennec] failed to fetch streaming token: %s", e)
            raise

    def _url_with_token(self, token: str) -> str:
        """Append/merge ?streaming_token=... to the WS URL safely."""
        parsed = urlparse(self._url)
        qs = dict(parse_qsl(parsed.query, keep_blank_values=True))
        qs["streaming_token"] = token
        new_query = urlencode(qs)
        return urlunparse(parsed._replace(query=new_query))

    async def start(
        self,
        on_final: Callable[[str], Awaitable[None]],
        on_partial: Optional[Callable[[str], Awaitable[None]]] = None,  # reserved for future partials
        on_vad: Optional[Callable[[dict], Awaitable[None]]] = None,
    ):
        if self._ws is not None:
            return

        self._on_final = on_final
        self._on_vad = on_vad

        # Obtain a short-lived streaming token and build the WS URL.
        logger.info("[fennec] requesting streaming token from %s", self._token_service_url)
        token = await self._fetch_streaming_token()
        ws_url = self._url_with_token(token)
        logger.info("[fennec] connect %s", ws_url)

        # Connect WITHOUT sending the API key header; auth is via the streaming token in the URL.
        self._ws = await ws_connect(
            ws_url,
            compression=None,
            max_size=None,
            ping_interval=self._ping_interval,
            open_timeout=15,
        )

        # Kick off receiver first so we can catch 'ready' immediately after we send 'start'
        self._recv_task = asyncio.create_task(self._recv_loop(), name="fennec_recv")

        # Start message mirrors your frontend flags
        start_msg = {
            "type": "start",
            "sample_rate": self._sr,
            "channels": self._ch,
            "single_utterance": False,
            "vad": self._vad,
        }

        if self._detect_thoughts:
            start_msg.update(
                {
                    "detect_thoughts": True,
                    "end_thought_eagerness": self._end_thought_eagerness,  # "low" | "medium" | "high"
                    "force_complete_time": float(self._force_complete_time),  # seconds
                }
            )

        if self._context:
            start_msg["context"] = self._context

        # ensure events requested
        self._vad.setdefault("events", True)
        self._vad.setdefault("event_hz", 8)

        await self._ws.send(json.dumps(start_msg))
        logger.info(
            "[fennec] started; sent config (thoughts=%s, drop_dead=%ss)",
            self._detect_thoughts,
            self._force_complete_time,
        )

        # IMPORTANT: wait until server says 'ready' before allowing send_pcm
        await asyncio.wait_for(self._ready.wait(), timeout=10.0)

    async def send_pcm(self, pcm_le16: bytes) -> None:
        await self._ready.wait()
        if not self._ws or self._closed:
            return
        try:
            await self._ws.send(pcm_le16)
        except Exception as e:
            logger.warning("[fennec] send error: %s", e)

    async def _recv_loop(self):
        assert self._ws is not None
        try:
            async for msg in self._ws:
                if isinstance(msg, (bytes, bytearray)):
                    continue

                try:
                    data = json.loads(msg)
                except Exception:
                    continue

                # Handle server handshake
                if data.get("type") == "ready":
                    self._ready.set()
                    logger.info("[fennec] server ready")
                    continue

                if e := data.get("error"):
                    logger.error("[fennec][error] %s", e)
                    continue

                mtype = data.get("type")
                if mtype in ("vad", "utterance"):
                    if self._on_vad:
                        try:
                            await self._on_vad(data)
                        except Exception:
                            logger.exception("[fennec] on_vad raised")
                    continue

                text = (data.get("text") or "").strip()
                if not text:
                    if "debug" in data:
                        logger.debug("[fennec][debug] %s", data["debug"])
                    continue

                if mtype in ("complete_thought", "corrected_transcript", "final_transcript", None):
                    if self._on_final:
                        try:
                            await self._on_final(text)
                        except Exception:
                            logger.exception("[fennec] on_final raised")
                # ignore partials by design

        except ConnectionClosed as e:
            logger.info("[fennec] connection closed by server (code=%s)", getattr(e, "code", "?"))
        except Exception as e:
            logger.warning("[fennec] recv error: %s", e)

    async def stop(self):
        if self._closed:
            return
        self._closed = True
        try:
            if self._ws:
                try:
                    await self._ws.send('{"type":"eos"}')
                except Exception:
                    pass
                await self._ws.close()
        except Exception:
            pass

        if self._recv_task:
            try:
                await asyncio.wait_for(self._recv_task, timeout=1.5)
            except Exception:
                self._recv_task.cancel()

        self._ws = None
        self._recv_task = None
        self._on_final = None
        self._on_vad = None
        self._ready = asyncio.Event()
        logger.info("[fennec] stopped")

    async def close(self):
        await self.stop()

    # -----------------------
    # Control-frame helpers
    # -----------------------

    async def _send_control(self, obj: dict) -> None:
        """Send a JSON control frame (ai_context / thought_packet)."""
        await self._ready.wait()
        if not self._ws or self._closed:
            return
        try:
            await self._ws.send(json.dumps(obj))
        except Exception as e:
            logger.debug("[fennec] control send failed: %s", e)

    async def send_ai_context(self, text: str) -> None:
        """
        Send the assistant's current/last message to prime thought detection.
        No-op if detect_thoughts=False or text is empty.
        """
        if not self._detect_thoughts:
            return
        text = (text or "").strip()
        if not text:
            return
        await self._send_control({"type": "ai_context", "text": text})

    async def send_thought_packet(self, ai_text: str, user_text: Optional[str] = None) -> None:
        """
        Optional richer control frame that includes both sides.
        Useful if you want to pass a small user fragment alongside the AI reply.
        """
        if not self._detect_thoughts:
            return
        ai = (ai_text or "").strip()
        ut = (user_text or "").strip() if isinstance(user_text, str) else None
        if not ai and not ut:
            return
        payload = {"type": "thought_packet", "ai_text": ai}
        if ut:
            payload["user_text"] = ut
        await self._send_control(payload)
