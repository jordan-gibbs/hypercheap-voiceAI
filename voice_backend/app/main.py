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
    await ws.send_text(StatusEvent(message="connected").model_dump_json())

    # Construct components
    fennec = FennecWSClient(
        api_key=settings.fennec_api_key,
        sample_rate=settings.fennec_sample_rate,
        channels=settings.fennec_channels,
        vad=DEFAULT_VAD,  # IMPORTANT: request VAD events + cadence
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

    async def on_asr_final(text: str):
        await ws.send_text(AsrFinalEvent(text=text).model_dump_json())

    async def on_llm_token(tok: str):
        await ws.send_text(LlmTokenEvent(text=tok).model_dump_json())

    async def on_tts_chunk(b: bytes):
        await ws.send_bytes(b)

    async def on_segment_done():
        await ws.send_text(SegmentDoneEvent().model_dump_json())

    async def on_audio_start():
        await ws.send_text(AudioStartEvent().model_dump_json())

    async def on_turn_done():
        await ws.send_text(TurnDoneEvent().model_dump_json())

    async def on_vad(evt: dict):
        # Forward raw VAD/utterance events to the client (UI meters, speaking state, etc.)
        await ws.send_text(json.dumps(evt))
        # Barge-in: when user starts speaking, interrupt AI output immediately
        try:
            is_speech = evt.get("type") == "vad" and evt.get("state") == "speech"
            utter_begin = evt.get("type") == "utterance" and evt.get("phase") == "begin"
            if is_speech or utter_begin:
                await agent.barge_in()
        except Exception:
            log.exception("barge-in interrupt failed")

    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.receive":
                if "bytes" in msg and msg["bytes"] is not None:
                    if session_started:
                        await agent.feed_pcm(msg["bytes"])
                elif "text" in msg and msg["text"]:
                    try:
                        payload = json.loads(msg["text"])
                        if payload.get("type") == "start":
                            if session_started:
                                continue
                            await ws.send_text(StatusEvent(message="initializing").model_dump_json())
                            await agent.start(
                                on_asr_final=on_asr_final,
                                on_token=on_llm_token,
                                on_audio_chunk=on_tts_chunk,
                                on_segment_done=on_segment_done,
                                on_audio_start=on_audio_start,
                                on_turn_done=on_turn_done,
                                on_vad=on_vad,  # wire VAD events through session -> fennec
                            )
                            session_started = True
                            await ws.send_text(StatusEvent(message="ready").model_dump_json())

                        elif payload.get("type") == "stop":
                            if session_started:
                                await agent.stop()
                            await ws.send_text(DoneEvent().model_dump_json())
                            break
                    except Exception as e:
                        log.exception("Error processing client message")
                        await ws.send_text(StatusEvent(message=f"error: {e}").model_dump_json())
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
