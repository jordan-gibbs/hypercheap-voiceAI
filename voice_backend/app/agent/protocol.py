from typing import Literal, Optional
from pydantic import BaseModel

class StartMessage(BaseModel):
    type: Literal["start"]
    voice_ref_id: Optional[str] = None
    latency: Optional[Literal["normal", "balanced"]] = None
    format: Optional[Literal["opus", "mp3", "wav"]] = None


class StopMessage(BaseModel):
    type: Literal["stop"]


WsInbound = StartMessage | StopMessage


class StatusEvent(BaseModel):
    type: Literal["status"] = "status"
    message: str


class AsrFinalEvent(BaseModel):
    type: Literal["asr_final"] = "asr_final"
    text: str


class LlmTokenEvent(BaseModel):
    type: Literal["llm_token"] = "llm_token"
    text: str


class SegmentDoneEvent(BaseModel):
    type: Literal["segment_done"] = "segment_done"


class TurnDoneEvent(BaseModel):
    type: Literal["turn_done"] = "turn_done"


class DoneEvent(BaseModel):
    type: Literal["done"] = "done"


class AudioStartEvent(BaseModel):
    type: Literal["audio_start"] = "audio_start"
