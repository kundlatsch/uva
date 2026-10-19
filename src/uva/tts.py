"""Text to speech for optional voice replies (VOICE_REPLIES).

``off`` (default): never. ``voice_in``: the reply to a voice message also
comes as a voice note. ``always``: every reply does. Uses OpenAI TTS
(TTS_MODEL, TTS_VOICE) in Opus, which Telegram plays as a voice note; the cost
(TTS_PRICE_PER_1K_CHARS) goes into the interactive budget.
"""

from __future__ import annotations

import re
import time

import openai

from uva.config import Settings
from uva.llm.budget import Scope, UsageRecord, UsageStore
from uva.llm.openai_keys import OPENAI_BASE_URL, resolve_openai_key
from uva.logging import get_logger
from uva.logging.events import EventType, current_trace_id, emit

log = get_logger(__name__)

MAX_CHARS = 1500  # longer replies are read only up to here
_MARKUP = re.compile(r"[*_`#>\[\]]|https?://\S+")


class SpeechError(RuntimeError):
    pass


def speakable(text: str) -> str:
    """The reply without Markdown marks and links, cut at a sentence near MAX_CHARS."""
    flat = " ".join(_MARKUP.sub("", text).split())
    if len(flat) <= MAX_CHARS:
        return flat
    cut = flat[:MAX_CHARS]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return cut[: end + 1] if end > MAX_CHARS // 2 else cut


def should_speak(mode: str, voice_in: bool) -> bool:
    return mode == "always" or (mode == "voice_in" and voice_in)


class Speaker:
    def __init__(
        self, api_key: str, *, model: str, voice: str, price_per_1k_chars: float,
        usage: UsageStore | None = None, base_url: str = OPENAI_BASE_URL, client=None,
    ) -> None:
        self.model = model
        self.voice = voice
        self.price = price_per_1k_chars
        self.usage = usage
        self.client = client or openai.AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=60)

    async def speak(self, text: str, scope: Scope = Scope.INTERACTIVE) -> bytes:
        text = speakable(text)
        if not text:
            raise SpeechError("nothing to say")
        started = time.perf_counter()
        try:
            response = await self.client.audio.speech.create(
                model=self.model, voice=self.voice, input=text, response_format="opus"
            )
        except openai.APIError as exc:
            await emit(EventType.ERROR, where="tts", model=self.model, error=str(exc))
            raise SpeechError(str(exc)) from exc
        audio = response.content if hasattr(response, "content") else bytes(response)
        cost = len(text) / 1000 * self.price
        if self.usage is not None:
            try:
                await self.usage.add(UsageRecord(
                    provider="openai", model=self.model, input_tokens=0, output_tokens=0, cost_usd=cost,
                    scope=scope, purpose="tts", trace_id=current_trace_id(),
                ))
            except Exception as exc:
                log.warning("usage_store_unavailable", error=str(exc))
        await emit(
            EventType.LLM_CALL, provider="openai", model=self.model, kind="tts", purpose="tts",
            scope=scope.value, input_tokens=0, output_tokens=0, chars=len(text), cost_usd=round(cost, 6),
            price_known=True, duration_ms=round((time.perf_counter() - started) * 1000),
        )
        return audio


def build_speaker(settings: Settings, usage: UsageStore | None) -> Speaker | None:
    if settings.voice_replies == "off":
        return None
    key = resolve_openai_key(settings, settings.tts_api_key or settings.transcribe_api_key)
    if not key:
        log.warning("voice_replies_disabled", reason="no OpenAI key (TTS_API_KEY)")
        return None
    return Speaker(
        key, model=settings.tts_model, voice=settings.tts_voice,
        price_per_1k_chars=settings.tts_price_per_1k_chars, usage=usage,
    )
