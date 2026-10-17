import json
import shutil
import subprocess
from types import SimpleNamespace

import httpx
import openai
import pytest
from fakes import InMemoryUsageStore, make_settings, with_llm
from pydantic import SecretStr

from uva.audio import Transcriber, build_transcriber, to_mp3
from uva.llm.budget import Budget, Scope, UsageRecord
from uva.logging.events import EventType
from uva.router import Router


class FakeTranscriptions:
    def __init__(self, text="pesquisa sobre a história do café", fail_models=()):
        self.text = text
        self.fail_models = set(fail_models)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs["model"] in self.fail_models:
            response = httpx.Response(400, request=httpx.Request("POST", "http://x"))
            raise openai.BadRequestError("model not available", response=response, body=None)
        return SimpleNamespace(text=self.text)


async def fake_converter(data: bytes) -> bytes:
    return b"mp3:" + data


def transcriber(fake, usage=None, **kw) -> Transcriber:
    client = SimpleNamespace(audio=SimpleNamespace(transcriptions=fake))
    return Transcriber(
        "k", model=kw.get("model", "gpt-4o-mini-transcribe"), fallback_model="whisper-1",
        language="pt", usage=usage, client=client, converter=fake_converter,
    )


@pytest.fixture
def audio_services(services):
    usage = InMemoryUsageStore()
    services.budget = Budget(usage, services.settings, services.tz)
    return services, usage


async def test_voice_is_transcribed_and_routed_like_text(audio_services, event_store):
    services, usage = audio_services
    fake = FakeTranscriptions(text="/ping")
    services.transcriber = transcriber(fake, usage)

    heard, reply = await Router(services).handle_audio(b"ogg-bytes", seconds=12)

    assert heard.text == "🎙️ entendi: /ping"
    assert reply.text == "pong"  # slash commands work by voice too
    call = fake.calls[0]
    assert call["model"] == "gpt-4o-mini-transcribe" and call["language"] == "pt"
    assert call["file"] == ("audio.mp3", b"mp3:ogg-bytes", "audio/mpeg")
    # 12 seconds at $0.003/min
    assert usage.records[0].purpose == "transcribe"
    assert usage.records[0].cost_usd == pytest.approx(12 / 60 * 0.003)
    llm_event = event_store.of_type(EventType.LLM_CALL)[0]
    assert llm_event.data["kind"] == "transcribe" and llm_event.data["audio_seconds"] == 12


async def test_transcript_goes_through_the_llm_and_memory(audio_services):
    services, _ = audio_services
    services.transcriber = transcriber(FakeTranscriptions(text="qual a capital da França?"))
    provider, _ = with_llm(services, json.dumps({"actions": [], "reply": "Paris, ué."}))

    from fakes import FakeBusy
    from test_stm import InMemorySessionBackend

    from uva.stm.service import ShortTermMemory

    services.busy = FakeBusy()
    services.stm = ShortTermMemory(InMemorySessionBackend(), services.settings)
    heard, reply = await Router(services).handle_audio(b"x", seconds=3)
    assert reply.text == "Paris, ué."
    assert provider.calls[0]["messages"][-1].content == "qual a capital da França?"
    turns = (await services.stm.session()).turns
    assert turns[-2].text == "[voz] qual a capital da França?"


async def test_fallback_model_when_main_one_is_unavailable(audio_services):
    services, usage = audio_services
    fake = FakeTranscriptions(fail_models={"gpt-4o-mini-transcribe"})
    services.transcriber = transcriber(fake, usage)
    heard, _ = await Router(services).handle_audio(b"x", seconds=60)
    assert [c["model"] for c in fake.calls] == ["gpt-4o-mini-transcribe", "whisper-1"]
    assert usage.records[0].model == "whisper-1"
    assert usage.records[0].cost_usd == pytest.approx(0.006)


async def test_long_transcript_is_clipped_in_the_echo(audio_services):
    services, _ = audio_services
    services.transcriber = transcriber(FakeTranscriptions(text="ping " * 400))
    heard, _ = await Router(services).handle_audio(b"x", seconds=60)
    assert len(heard.text) < 850 and heard.text.endswith("…")


async def test_missing_key_and_budget(audio_services):
    services, usage = audio_services
    services.transcriber = None
    [reply] = await Router(services).handle_audio(b"x")
    assert "falta uma chave da OpenAI" in reply.text

    fake = FakeTranscriptions()
    services.transcriber = transcriber(fake, usage)
    usage.records.append(
        UsageRecord(provider="p", model="m", input_tokens=0, output_tokens=0, purpose="t",
                    cost_usd=services.settings.llm_daily_budget_usd, scope=Scope.INTERACTIVE)
    )
    [reply] = await Router(services).handle_audio(b"x")
    assert "orçamento diário" in reply.text and fake.calls == []


def test_key_resolution():
    assert build_transcriber(make_settings(llm_provider="anthropic", llm_api_key="sk-ant"), None) is None
    main = make_settings(llm_provider="openai_compatible", llm_api_key="sk-o", llm_base_url="https://api.openai.com/v1")
    assert build_transcriber(main, None) is not None
    assert build_transcriber(make_settings(transcribe_api_key=SecretStr("sk-t")), None) is not None


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
async def test_ffmpeg_converts_ogg_opus_to_mp3(tmp_path):
    ogg = tmp_path / "tone.ogg"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-c:a", "libopus", str(ogg)],
        check=True,
    )
    mp3 = await to_mp3(ogg.read_bytes())
    assert mp3[:3] == b"ID3" or mp3[0] == 0xFF  # MP3 header


async def test_voice_echo_in_english(audio_services):
    from uva import i18n

    i18n.set_language("en")
    services, usage = audio_services
    services.transcriber = transcriber(FakeTranscriptions(text="/ping"), usage)
    heard, reply = await Router(services).handle_audio(b"ogg-bytes", seconds=3)
    assert heard.text == "🎙️ heard: /ping"
    assert reply.text == "pong"
