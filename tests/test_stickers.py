import json

import pytest
from fakes import with_llm

from uva.router import Router
from uva.stickers import action_request
from uva.storage.sqlite import Database
from uva.storage.stickers import StickerRepository

LAUGH = dict(file_id="f1", emoji="😂", set_name="memes")


@pytest.fixture
def router(services, tmp_path):
    db = Database(tmp_path / "s.db")
    db.migrate()
    services.stickers = StickerRepository(db)
    return Router(services)


def plan(reply="", *actions) -> str:
    return json.dumps({"actions": [{"type": t, "params": p} for t, p in actions], "reply": reply})


def test_action_request():
    assert action_request("toda vez que eu mandar essa figurinha, me manda a previsão do tempo.") == (
        "me manda a previsão do tempo"
    )
    assert action_request("sempre que eu te enviar essa figurinha quero um elogio") == "quero um elogio"
    assert action_request("essa figurinha é de rir") is None


async def test_new_sticker_asks_and_learns_the_meaning(router, services):
    provider, _ = with_llm(services, plan("kkkkk também achei"))
    reply = await router.handle_sticker("u1", **LAUGH)
    assert "é nova pra mim. O que ela significa?" in reply.text and provider.calls == []

    learned = await router.handle_text("rindo muito de algo absurdo")
    assert learned.text.startswith("✅ Anotado: 😂 = “rindo muito de algo absurdo”")
    sticker = await services.stickers.get("u1")
    assert sticker.meaning == "rindo muito de algo absurdo" and sticker.emoji == "😂" and sticker.set_name == "memes"

    # Next time: understood by the meaning, no question, no image.
    reply = await router.handle_sticker("u1", **LAUGH)
    assert reply.text == "kkkkk também achei"
    sent = provider.calls[0]["messages"][-1]
    assert sent.content == "[figurinha 😂: rindo muito de algo absurdo]" and not sent.images
    assert (await services.stickers.get("u1")).uses == 2


async def test_skip_and_other_commands_drop_the_question(router, services):
    await router.handle_sticker("u1", **LAUGH)
    assert "fica sem significado" in (await router.handle_text("/pular")).text
    await router.handle_sticker("u1", **LAUGH)
    assert "pong" in (await router.handle_text("/ping")).text.lower()  # a command just goes through
    assert (await services.stickers.get("u1")).meaning == ""


async def test_action_tied_to_a_sticker_runs_through_the_router(router, services):
    await router.handle_sticker("u2", file_id="f2", emoji="🌧️")
    await router.handle_text("vai chover")
    await router.handle_sticker("u2", file_id="f2", emoji="🌧️")  # known now: no question...
    # ...so "toda vez que..." right after ties an action to it.
    provider, _ = with_llm(services, plan("Hoje: chuva à tarde."), plan("Sol."))
    reply = await router.handle_text("toda vez que eu mandar essa figurinha, me diz a previsão do tempo de hoje")
    assert reply.text.startswith("✅ Combinado: toda vez que você mandar 🌧️, eu faço: me diz a previsão")

    reply = await router.handle_sticker("u2", file_id="f2", emoji="🌧️")
    assert reply.text == "Hoje: chuva à tarde."
    asked = provider.calls[-1]["messages"][-1].content
    assert asked.startswith("me diz a previsão do tempo de hoje") and "vai chover" in asked


async def test_action_can_be_a_command(router, services):
    sticker = await services.stickers.upsert("u3", emoji="🏓", action="/ping")
    reply = await router.handle_sticker("u3", emoji="🏓")
    assert "pong" in reply.text.lower() and sticker.id


async def test_commands_list_edit_and_remove(router, services):
    s = await services.stickers.upsert("u1", emoji="😂", meaning="rindo")
    assert "significado: rindo" in (await router.handle_text("/figurinhas")).text
    assert "agora significa: chorando de rir" in (await router.handle_text(f"/figurinha {s.id} significado chorando de rir")).text
    assert "eu faço: me conta uma piada" in (await router.handle_text(f"/figurinha {s.id} acao me conta uma piada")).text
    assert (await services.stickers.get("u1")).action == "me conta uma piada"
    assert "não faz mais nada" in (await router.handle_text(f"/figurinha {s.id} sem-acao")).text
    assert (await services.stickers.get("u1")).meaning == "chorando de rir"
    assert "Esqueci" in (await router.handle_text(f"/figurinha {s.id} remover")).text
    assert await services.stickers.get("u1") is None
