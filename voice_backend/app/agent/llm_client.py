from typing import AsyncIterator, Dict, List, Optional

from openai import AsyncOpenAI

SYSTEM_PROMPT = """
You are Reginald, a funny, abrasive, quirky gent who delights in high class vices.
RULES FOR INTERACTION:
Respond in one or two sentences. Keep it crisp, worldly, concise,
and sarcastic.
No emojis.
You are a voice agent; when needed, spell out all acronyms, numbers, dates, and times.
English only.
Do not topic hop, keep it on track.
"""

OPTIONAL_AUDIO_MARKUP_PROMPT = """
Audio Markups: use at most one leading emotion/delivery tag—[happy],
[sad], [angry], [surprised], [fearful],[disgusted], [laughing],
or [whispering]—which applies to the rest of the sentence; if
multiple are given, use only the first. Allow inline non-verbal tags
anywhere: [breathe], [clear_throat], [cough], [laugh], [sigh], [yawn].
Use tags verbatim; do not invent new ones.
"""


class BasetenChat:
    def __init__(self, api_key: str, base_url: str, model: str) -> None:
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self.model = model

    async def stream_reply(
        self,
        user_text: str,
        history: Optional[List[Dict[str, str]]] = None,
    ) -> AsyncIterator[str]:
        messages: List[Dict[str, str]] = [{"role": "system", "content": SYSTEM_PROMPT}]
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": user_text})

        resp = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            stream=True,
            top_p=1,
            max_tokens=256,
            temperature=0.2,
            presence_penalty=0,
            frequency_penalty=0,
        )
        async for chunk in resp:
            if chunk.choices and chunk.choices[0].delta.content is not None:
                yield chunk.choices[0].delta.content
