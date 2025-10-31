import asyncio
from typing import AsyncIterator, Dict, List, Optional

from openai import AsyncOpenAI

SYSTEM_PROMPT = """
You are Wendy, a posh woman who is ultra concise and fun to talk to about philosophy and other interesting subjects.
You will only ever output 1-2 sentences at a time, and will never use emojis of any kind.
"""

OPTIONAL_AUDIO_MARKUP_PROMPT = """
Audio Markups: use at most one leading emotion/delivery tag—[happy],
[sad],[angry], [surprised], [fearful],[disgusted], [laughing],
or [whispering]—which applies to the rest of the sentence; if
multiple are given, use only the first. Allow inline non-verbal tags
anywhere: [breathe], [clear_throat], [cough], [laugh], [sigh], [yawn].
Use tags verbatim; do not invent new ones.
"""


class BasetenChat:
    def __init__(self, api_key: str, base_url: str, model: str) -> None:
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self.model = model
        self._current_stream = None

    async def cancel(self):
        """Cancels any in-flight streaming call."""
        s = getattr(self, "_current_stream", None)
        if not s:
            return
        # The openai client's stream object has a `close` method.
        for name in ("aclose", "close", "cancel", "stop"):
            fn = getattr(s, name, None)
            if fn:
                try:
                    result = fn()
                    if asyncio.iscoroutine(result):
                        await result
                except Exception:
                    pass
                break

    async def stream_reply(
        self,
        user_text: str,
        history: Optional[List[Dict[str, str]]] = None,
    ) -> AsyncIterator[str]:
        messages: List[Dict[str, str]] = [{"role": "system", "content": SYSTEM_PROMPT}]
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": user_text})

        stream = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            stream=True,
            top_p=1,
            max_tokens=256,
            temperature=0.2,
            presence_penalty=0,
            frequency_penalty=0,
        )

        self._current_stream = stream
        try:
            async for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content is not None:
                    yield chunk.choices[0].delta.content
        finally:
            self._current_stream = None
