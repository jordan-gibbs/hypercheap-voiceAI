import contextlib
import json
import logging
import os

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .agent.fennec_ws import DEFAULT_VAD, FennecWSClient
from .agent.inworld_tts import InworldTTS
from .agent.llm_client import BasetenChat
from .agent.protocol import (
    AsrFinalEvent,
    AudioStartEvent,
    DoneEvent,
    LlmTokenEvent,
    SegmentDoneEvent,
    StatusEvent,
    TurnDoneEvent,
)
from .agent.session import AgentSession
from .config import settings

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("hypercheap.app")

app = FastAPI(title="Hypercheap Voice Agent")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

static_files_path = os.path.join(os.path.dirname(__file__), "static")


@app.get("/health")
async def health():
    return {"ok": True}


@app.websocket("/ws/agent")
async def ws_agent(ws: WebSocket):
    await ws.accept()
    # Synchronization: Send "connected". We wait for the client "start" message to initialize services.
    await ws.send_text(StatusEvent(message="connected").model_dump_json())

    fennec = FennecWSClient(
        api_key=settings.fennec_api_key,
        sample_rate=settings.fennec_sample_rate,
        channels=settings.fennec_channels,
        vad=DEFAULT_VAD,
    )
    llm = BasetenChat(
        api_key=settings.baseten_api_key,
        base_url=settings.baseten_base_url,
        model=settings.baseten_model,
    )
    tts = InworldTTS(
        api_key_basic_b64=settings.inworld_api_key,
        model_id=settings.inworld_model_id,
        voice_id=settings.inworld_voice_id,
        sample_rate_hz=settings.inworld_sample_rate,
    )
    agent = AgentSession(fennec, llm, tts)
    session_started = False

    # --- Outbound event bridges to the client
    async def on_asr_final(text: str):
        await ws.send_text(AsrFinalEvent(text=text).model_dump_json())

    async def on_llm_token(tok: str):
        # stream tokens so the UI can show draft text live
        await ws.send_text(LlmTokenEvent(text=tok).model_dump_json())

    async def on_tts_chunk(b: bytes):
        # raw PCM16 bytes @ 48k; client streams it via AudioWorklet
        await ws.send_bytes(b)

    async def on_segment_done():
        await ws.send_text(SegmentDoneEvent().model_dump_json())

    async def on_audio_start():
        await ws.send_text(AudioStartEvent().model_dump_json())

    # NEW: Used for accurate frontend labeling (signals LLM completion)
    async def on_turn_done():
        await ws.send_text(TurnDoneEvent().model_dump_json())

    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.receive":
                if "bytes" in msg and msg["bytes"] is not None:
                    # Only feed audio if the session has successfully started
                    if session_started:
                        await agent.feed_pcm(msg["bytes"])
                elif "text" in msg and msg["text"]:
                    try:
                        payload = json.loads(msg["text"])
                        if payload.get("type") == "start":
                            if session_started:
                                continue
                            # Synchronization: Start the agent (connects to Fennec, sends VAD config)
                            await ws.send_text(StatusEvent(message="initializing").model_dump_json())
                            await agent.start(
                                on_asr_final=on_asr_final,
                                on_token=on_llm_token,
                                on_audio_chunk=on_tts_chunk,
                                on_segment_done=on_segment_done,
                                on_audio_start=on_audio_start,
                                on_turn_done=on_turn_done,
                            )
                            # Fennec is ready and VAD config is sent (awaited in agent.start).
                            session_started = True
                            # Signal the frontend that the session is fully ready.
                            await ws.send_text(StatusEvent(message="ready").model_dump_json())

                        elif payload.get("type") == "stop":
                            # Gracefully stop: finish current turn if any, then announce done
                            if session_started:
                                await agent.stop()
                            await ws.send_text(DoneEvent().model_dump_json())
                            break
                    except Exception as e:
                        log.exception("Error processing client message")
                        await ws.send_text(StatusEvent(message=f"error: {e}").model_dump_json())
                        # If initialization failed, break the loop to close the session.
                        if payload.get("type") == "start" and not session_started:
                            break
            elif msg["type"] == "websocket.disconnect":
                break
    except WebSocketDisconnect:
        pass
    finally:
        with contextlib.suppress(Exception):
            await agent.close()
        with contextlib.suppress(Exception):
            await ws.close()


app.mount("/", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static"), html=True), name="static")
