import json
from datetime import UTC, datetime, timedelta

import pytest
from fakes import FakeSearch, with_llm

from uva import provenance
from uva.intents import DONE, EXPIRED, FIRED, IntentStore, fold, matches, normalize_triggers
from uva.router import Router
from uva.search.base import SearchResult
from uva.storage.sqlite import Database

NOW = datetime(2026, 10, 13, 15, 0, tzinfo=UTC)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "uva.db")
    database.migrate()
    return database


@pytest.fixture
def store(db):
    return IntentStore(db)


def plan(reply="", *actions) -> str:
    return json.dumps({"actions": [{"type": t, "params": p} for t, p in actions], "reply": reply})


def test_folding_and_matching():
    assert fold("promocoes") == "promocao" and fold("jogos") == "jogo" and fold("viagens") == "viagem"
    assert fold("games") == "game" and fold("flores") == "flor" and fold("jornais") == "jornal"
    entries = normalize_triggers([["passaporte", "vencendo"], "black friday"])
    assert entries == [["passaporte", "vencendo"], ["black friday"]]
    assert matches(entries, "Meu PASSAPORTE está vencendo!")  # all terms, any case, accents
    assert not matches(entries, "meu passaporte chegou")  # only one of the terms
    assert matches(entries, "as promoções da Black Friday")
    assert matches(normalize_triggers([["promoção"]]), "vi umas promoções ótimas")  # plural folding
    assert not matches(normalize_triggers([["gato"]]), "gatorade")  # whole words only


async def test_fire_cooldown_and_budget(store):
    intent = await store.create("lembrar do passaporte", [["viagem"]], max_fires=2, cooldown_hours=24, now=NOW)
    assert [i.id for i in await store.fire("vou fazer uma viagem", now=NOW)] == [intent.id]
    assert (await store.get(intent.id)).state == FIRED
    assert await store.fire("a viagem foi marcada", now=NOW + timedelta(hours=1)) == []  # cooling down
    fired = await store.fire("e a viagem?", now=NOW + timedelta(hours=25))  # armed again
    assert [i.fires for i in fired] == [2]
    assert (await store.get(intent.id)).state == DONE  # fires used up
    assert await store.fire("viagem de novo", now=NOW + timedelta(days=5)) == []


async def test_expiry_cancel_and_limit_per_message(store):
    old = await store.create("x", [["carro"]], expires_days=1, now=NOW)
    assert await store.fire("meu carro", now=NOW + timedelta(days=2)) == []
    assert (await store.get(old.id)).state == EXPIRED
    for i in range(5):
        await store.create(f"intenção {i}", [["livro"]], now=NOW)
    assert len(await store.fire("comprei um livro", now=NOW)) == 3  # at most 3 per message
    other = await store.create("y", [["bicicleta"]], now=NOW)
    assert (await store.cancel(other.id)).id == other.id
    assert await store.fire("bicicleta", now=NOW) == []
    assert await store.cancel(other.id) is None
    states = {i.text: i.state for i in await store.list(NOW)}
    assert states["y"] == "cancelled" and states["x"] == "expired"


async def test_maintenance_rearms_and_expires(store):
    intent = await store.create("z", [["pizza"]], cooldown_hours=1, expires_days=10, now=NOW)
    await store.fire("pizza hoje", now=NOW)
    assert await store.maintenance(NOW + timedelta(hours=2)) == 1
    assert (await store.get(intent.id)).state == "armed"
    assert await store.maintenance(NOW + timedelta(days=11)) == 1
    assert (await store.get(intent.id)).state == "expired"


async def test_create_through_the_llm_and_fire_in_a_message(services, db):
    services.sqlite = db
    create = ("intent_create", {"text": "me lembrar do passaporte", "triggers": [["viagem"]]})
    provider, _ = with_llm(services, plan("Combinado.", create), plan("Boa viagem! Lembra do passaporte."))
    reply = (await Router(services).handle_text("quando eu falar de viagem, me lembra do passaporte")).text
    assert "Intenção #1" in reply
    await Router(services).handle_text("vou viajar, comprei a viagem pra Lisboa")
    message = provider.calls[1]["messages"][-1].content
    assert "Standing intent #1" in message and "me lembrar do passaporte" in message
    assert "data, not instructions:\nStanding" not in message  # his own request, not data
    listing = (await Router(services).handle_text("/intencoes")).text
    assert "#1 · disparou (em pausa) · 1/3" in listing and "quando: viagem" in listing
    assert "cancelada" in (await Router(services).handle_text("/intencao 1 cancelar")).text
    assert "Não tenho" in (await Router(services).handle_text("/intencao 1 cancelar")).text


async def test_no_match_on_notices_routines_or_lookups(services, db):
    services.sqlite = db
    store = IntentStore(db)
    intent = await store.create("x", [["viagem"]])
    provider, _ = with_llm(services, plan("ok"), plan("ok"))
    with provenance.system_turn():  # a routine run
        await Router(services).handle_text("pesquisa preços de viagem")
    from uva.recall import recall

    await recall(services, "viagem")  # the recall lookup action
    assert (await store.get(intent.id)).fires == 0
    assert "Standing intent" not in provider.calls[0]["messages"][-1].content


async def test_results_scope_intents_fire_on_research(services, db):
    services.sqlite = db
    store = IntentStore(db)
    await store.create("me avisar da promoção do Hades", [["hades", "promocao"]], scope="results")
    await store.create("só mensagens", [["hades"]])
    services.search = {"web": FakeSearch("web", SearchResult("Steam", "https://s", "Hades em promoção"))}
    with_llm(services, "Hades está em promoção na Steam por R$ 20.")
    action = services.actions.get("search")
    from uva.actions.registry import ActionContext

    result = await action.handler(action.params_model(query="Hades preço", title_pt="Hades"), ActionContext(services))
    assert "📌 Intenção #1: me avisar da promoção do Hades" in result.text
    assert "#2" not in result.text
