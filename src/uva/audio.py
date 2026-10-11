"""Speech to text for Telegram voice notes, audio files and video notes.

Telegram voice notes are OGG/Opus, which the OpenAI transcription API
doesn't accept, so everything is converted with ffmpeg to a small mono MP3
(16 kHz, 32 kbps) first. Transcription uses TRANSCRIBE_MODEL (default
gpt-4o-mini-transcribe) with a fallback model, and the cost is accounted per
minute of audio.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import openai

from uva.config import Settings
from uva.llm.budget import Scope, UsageRecord, UsageStore
from uva.llm.openai_keys import OPENAI_BASE_URL, resolve_openai_key
from uva.logging import get_logger
from uva.logging.events import EventType, current_trace_id, emit

log = get_logger(__name__)

TELEGRAM_DOWNLOAD_LIMIT = 20 * 1024 * 1024  # bots can't download bigger files
MP3_BITRATE = 32_000  # bits per second, used to estimate duration when unknown

# USD per minute (OpenAI pricing page); TRANSCRIBE_PRICE_PER_MINUTE overrides.
PRICE_PER_MINUTE = {
    "gpt-transcribe": 0.0045,
    "gpt-4o-transcribe": 0.006,
    "gpt-4o-mini-transcribe": 0.003,
    "whisper-1": 0.006,
}
DEFAULT_PRICE = 0.006


class TranscriptionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Transcript:
    text: str
    model: str
    seconds: float
    cost_usd: float


async def to_mp3(data: bytes, timeout: float = 120) -> bytes:
    """Any audio/video container -> mono 16 kHz MP3, via ffmpeg pipes."""
    try:
        process = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
            "-vn", "-ac", "1", "-ar", "16000", "-b:a", "32k", "-f", "mp3", "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise TranscriptionError("ffmpeg não está instalado") from exc
    try:
        out, err = await asyncio.wait_for(process.communicate(data), timeout)
    except TimeoutError as exc:
        process.kill()
        raise TranscriptionError("a conversão do áudio demorou demais") from exc
    if process.returncode != 0 or not out:
        raise TranscriptionError(f"não consegui converter o áudio: {err.decode(errors='replace')[-300:]}")
    return out


class Transcriber:
    def __init__(
        self,
        api_key: str,
        *,
        model: str,
        fallback_model: str | None,
        language: str | None,
        price_per_minute: float | None = None,
        base_url: str = OPENAI_BASE_URL,
        usage: UsageStore | None = None,
        client=None,
        converter=to_mp3,
        timeout: float = 120,
    ) -> None:
        self.model = model
        self.fallback_model = fallback_model
        self.language = language or None
        self.price_override = price_per_minute
        self.usage = usage
        self.converter = converter
        self.client = client or openai.AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=timeout)

    def price(self, model: str) -> float:
        if self.price_override is not None:
            return self.price_override
        return PRICE_PER_MINUTE.get(model, DEFAULT_PRICE)

    async def transcribe(
        self, data: bytes, *, seconds: float | None = None, scope: Scope = Scope.INTERACTIVE
    ) -> Transcript:
        mp3 = await self.converter(data)
        duration = seconds if seconds else len(mp3) * 8 / MP3_BITRATE
        started = time.perf_counter()

        models = [self.model] + ([self.fallback_model] if self.fallback_model else [])
        last_error: Exception | None = None
        for model in models:
            try:
                text = await self._call(model, mp3)
                break
            except (openai.BadRequestError, openai.NotFoundError, openai.PermissionDeniedError) as exc:
                last_error = exc  # model not available for this key: try the fallback
                log.warning("transcribe_model_failed", model=model, error=str(exc))
            except openai.APIError as exc:
                await emit(EventType.ERROR, where="transcribe", model=model, error=str(exc))
                raise TranscriptionError(str(exc)) from exc
        else:
            await emit(EventType.ERROR, where="transcribe", error=str(last_error))
            raise TranscriptionError(str(last_error))

        cost = duration / 60 * self.price(model)
        await self._account(model, duration, cost, scope, started, text)
        return Transcript(text.strip(), model, duration, cost)

    async def _call(self, model: str, mp3: bytes) -> str:
        kwargs = {"model": model, "file": ("audio.mp3", mp3, "audio/mpeg")}
        if self.language:
            kwargs["language"] = self.language
        result = await self.client.audio.transcriptions.create(**kwargs)
        return result if isinstance(result, str) else getattr(result, "text", "") or ""

    async def _account(self, model, seconds, cost, scope, started, text) -> None:
        if self.usage is not None:
            try:
                await self.usage.add(
                    UsageRecord(
                        provider="openai", model=model, input_tokens=0, output_tokens=0,
                        cost_usd=cost, scope=scope, purpose="transcribe", trace_id=current_trace_id(),
                    )
                )
            except Exception as exc:
                log.warning("usage_store_unavailable", error=str(exc))
        await emit(
            EventType.LLM_CALL,
            provider="openai",
            model=model,
            kind="transcribe",
            purpose="transcribe",
            scope=scope.value,
            input_tokens=0,
            output_tokens=0,
            audio_seconds=round(seconds, 1),
            cost_usd=round(cost, 6),
            price_known=True,
            duration_ms=round((time.perf_counter() - started) * 1000),
            response=text,
        )


def build_transcriber(settings: Settings, usage: UsageStore | None) -> Transcriber | None:
    key = resolve_openai_key(settings, settings.transcribe_api_key)
    if not key:
        return None
    return Transcriber(
        key,
        model=settings.transcribe_model,
        fallback_model=settings.transcribe_fallback_model or None,
        language=settings.transcribe_language,
        price_per_minute=settings.transcribe_price_per_minute,
        base_url=settings.transcribe_base_url or OPENAI_BASE_URL,
        usage=usage,
    )
