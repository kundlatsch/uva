import json
from datetime import UTC, datetime, timedelta

import pytest
from fakes import InMemoryMemoryStore, make_settings, with_llm

from uva.memory import MemoryCategory, MemoryItem, MemoryService, normalize_key, score
from uva.router import Router

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)


@pytest.fixture
def store() -> InMemoryMemoryStore:
    return InMemoryMemoryStore()


@pytest.fixture
def memory(store) -> MemoryService:
    return MemoryService(store, make_settings(memory_working_size=2))


def item(key, relevance=0.5, idle_days=0, uses=0) -> MemoryItem:
    return MemoryItem(
        key=key,
        value=key,
        relevance=relevance,
        last_used_at=NOW - timedelta(days=idle_days),
        use_count=uses,
    )


def test_normalize_key():
    assert normalize_key("Cidade Natal") == "cidade_natal"
    assert normalize_key("nome do gato") == normalize_key("Nome-do-Gato")


def test_score_combines_relevance_recency_and_frequency():
    fresh = score(item("a", relevance=1), NOW, half_life_days=30)
    month_old = score(item("a", relevance=1, idle_days=30), NOW, half_life_days=30)
    assert month_old == pytest.approx(fresh / 2)
    assert score(item("a", uses=10), NOW, 30) > score(item("a", uses=0), NOW, 30)
    assert score(item("a", relevance=0), NOW, 30) == 0


async def test_working_set_is_top_n(memory, store):
    for it in [item("low", 0.1), item("high", 1.0), item("mid", 0.5)]:
        await store.upsert(it)
    top = await memory.recompute_working_set(NOW)
    assert [i.key for i in top] == ["high", "mid"]
    assert store.working_set == (["high", "mid"], NOW)


async def test_working_set_is_recomputed_after_a_day(memory, store):
    await store.upsert(item("a", 0.9))
    await memory.recompute_working_set(NOW)
    await store.upsert(item("b", 1.0))
    # Same day: cached.
    assert [i.key for i in await memory.working_memory(NOW + timedelta(hours=1))] == ["a"]
    # Next day: recomputed.
    later = await memory.working_memory(NOW + timedelta(days=1, minutes=1))
    assert [i.key for i in later][0] == "b"


async def test_save_replaces_value_and_counts_use(memory):
    first = await memory.save("Cidade", "Curitiba")
    second = await memory.save("cidade", "Florianópolis")
    assert second.key == "cidade" and second.value == "Florianópolis"
    assert second.use_count == first.use_count + 1
    assert second.created_at == first.created_at


async def test_save_with_ttl_expires(memory):
    saved = await memory.save("viagem", "em Lisboa esta semana", ttl_days=7)
    assert saved.expires_at is not None
    assert saved.is_expired(saved.expires_at + timedelta(seconds=1))


async def test_maintenance_archives_stale_low_score_items(memory, store):
    await store.upsert(item("velho", relevance=0.1, idle_days=200))
    await store.upsert(item("velho_mas_importante", relevance=1.0, idle_days=100, uses=50))
    await store.upsert(item("recente", relevance=0.1, idle_days=1))
    result = await memory.maintenance(NOW)
    assert result["archived"] == 1
    assert [a.key for a, _ in store.archived] == ["velho"]
    assert "recente" in store.items and "velho_mas_importante" in store.items


async def test_memory_commands(services, memory):
    services.memory = memory
    router = Router(services)
    assert "cidade = Florianópolis" in (await router.handle_text("/lembrar cidade Florianópolis")).text
    listing = (await router.handle_text("/memoria")).text
    assert "★ cidade: Florianópolis" in listing
    assert "Florianópolis" in (await router.handle_text("/memoria cidade")).text
    assert "Esqueci" in (await router.handle_text("/esquecer cidade")).text
    assert "Não achei" in (await router.handle_text("/esquecer cidade")).text


async def test_llm_saves_memory_and_sees_it_next_time(services, memory):
    services.memory = memory
    save_plan = json.dumps(
        {
            "actions": [
                {
                    "type": "memory_save",
                    "params": {"key": "nome_do_gato", "value": "Mingau", "category": "fato"},
                }
            ],
            "reply": "Que nome fofo! Vou lembrar.",
        }
    )
    provider, _ = with_llm(services, save_plan, json.dumps({"actions": [], "reply": "Mingau."}))
    router = Router(services)

    reply = await router.handle_text("meu gato se chama Mingau")
    assert reply.text == "Que nome fofo! Vou lembrar."
    assert (await memory.get("nome_do_gato")).value == "Mingau"

    await router.handle_text("como se chama meu gato?")
    assert "- [fato] nome_do_gato: Mingau" in provider.calls[1]["system"]


async def test_invalid_category_rejects_plan(services, memory):
    services.memory = memory
    bad = json.dumps(
        {"actions": [{"type": "memory_save", "params": {"key": "x", "value": "y", "category": "segredo"}}]}
    )
    with_llm(services, bad)
    await Router(services).handle_text("guarda isso")
    assert await memory.get("x") is None


def test_categories():
    assert {c.value for c in MemoryCategory} == {"perfil", "preferencia", "fato", "contexto"}
