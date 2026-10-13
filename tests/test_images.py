import base64
import json
from zoneinfo import ZoneInfo

import pytest
from fakes import FakeBusy, with_llm
from test_stm import InMemorySessionBackend

from uva.brain.vault import Vault
from uva.llm.anthropic_provider import _content as anthropic_content
from uva.llm.base import LLMMessage
from uva.llm.openai_provider import _content as openai_content
from uva.media import data_url_bytes, parse_data_url, to_data_url
from uva.router import Router
from uva.stm.service import ShortTermMemory

PNG = to_data_url(b"\x89PNG fake", "image/png")


def test_provider_payloads():
    message = LLMMessage("user", "o que é isso?", images=(PNG,))
    assert openai_content(message) == [
        {"type": "text", "text": "o que é isso?"},
        {"type": "image_url", "image_url": {"url": PNG}},
    ]
    blocks = anthropic_content(message)
    assert blocks[0]["type"] == "image"
    assert blocks[0]["source"]["media_type"] == "image/png"
    assert base64.b64decode(blocks[0]["source"]["data"]) == b"\x89PNG fake"
    assert blocks[-1] == {"type": "text", "text": "o que é isso?"}
    assert openai_content(LLMMessage("user", "só texto")) == "só texto"


def test_data_urls():
    assert parse_data_url(PNG)[0] == "image/png"
    assert data_url_bytes(PNG) == ("image/png", b"\x89PNG fake")


@pytest.fixture
def img_services(services, tmp_path):
    services.busy = FakeBusy()
    services.stm = ShortTermMemory(InMemorySessionBackend(), services.settings)
    services.vault = Vault(tmp_path / "brain", ZoneInfo("America/Sao_Paulo"))
    services.vault.ensure_structure()
    return services


async def test_caption_goes_to_the_vision_model_with_the_image(img_services):
    services = img_services
    services.settings.llm_model_vision = None
    provider, _ = with_llm(services, json.dumps({"actions": [], "reply": "É um gato."}))
    reply = await Router(services).handle_photo(PNG, "o que é isso?")
    assert reply.text == "É um gato."
    last = provider.calls[0]["messages"][-1]
    assert last.images == (PNG,) and last.content == "o que é isso?"
    assert provider.calls[0]["model"] == "router-model"  # vision falls back to the router model


async def test_slash_caption_still_runs_the_command(img_services):
    reply = await Router(img_services).handle_photo(PNG, "/ping")
    assert reply.text == "pong"


async def test_uncaptioned_photo_is_described_and_kept(img_services):
    services = img_services
    provider, _ = with_llm(services, 'Uma placa escrita "PARE".')
    reply = await Router(services).handle_photo(PNG)
    assert reply.text.startswith('📷 Uma placa escrita "PARE".')
    call = provider.calls[0]
    assert call["messages"][0].images == (PNG,) and call["system"] is None
    session = await services.stm.session()
    assert session.refs[-1].tipo == "imagem" and "PARE" in session.refs[-1].rotulo
    assert services.last_photo.get() == PNG


async def test_followup_question_sees_the_image_again(img_services):
    services = img_services
    provider, _ = with_llm(services, "Uma placa.", json.dumps({"actions": [], "reply": "Diz PARE."}))
    router = Router(services)
    await router.handle_photo(PNG)
    reply = await router.handle_text("o que tá escrito aí?")
    assert reply.text == "Diz PARE."
    assert provider.calls[1]["messages"][-1].images == (PNG,)


async def test_unrelated_message_without_image_focus_has_no_image(services):
    services.last_photo.set(PNG)
    provider, _ = with_llm(services, json.dumps({"actions": [], "reply": "Paris."}))
    await Router(services).handle_text("qual a capital da França?")
    assert provider.calls[0]["messages"][-1].images == ()


async def test_image_save_action(img_services):
    services = img_services
    services.last_photo.set(PNG)
    plan = {
        "actions": [{"type": "image_save", "params": {"title": "Placa", "description": "Placa de pare"}}],
        "reply": "Salvei.",
    }
    with_llm(services, json.dumps(plan))
    reply = await Router(services).handle_text("salva essa foto no second brain")
    assert "🖼️ Imagem salva em 05-anexos/placa.png" in reply.text
    assert (services.vault.root / "05-anexos" / "placa.png").read_bytes() == b"\x89PNG fake"
    note = services.vault.read("Placa")
    assert "![[05-anexos/placa.png]]" in note.body and "Placa de pare" in note.body
    # No title from the LLM: one is derived from the description.
    plan["actions"][0]["params"] = {"description": "Casinha vermelha com árvore e sol"}
    with_llm(services, json.dumps(plan))
    reply = await Router(services).handle_text("salva essa foto")
    assert "05-anexos/casinha_vermelha_com_arvore_e_sol.png" in reply.text
    # Saving again never overwrites.
    assert services.vault.save_attachment(b"x", "png", "Placa") == "05-anexos/placa_2.png"
