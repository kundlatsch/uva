import json
import os
from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest
from fakes import InMemoryGoalStore, InMemoryMemoryStore, make_settings, with_llm

from uva.actions.registry import ActionContext
from uva.brain.vault import Vault
from uva.clock import utcnow
from uva.goals.store import Goal, GoalResult
from uva.index.service import SearchIndex, notice_source, turn_source
from uva.memory import MemoryService
from uva.recall import past_window, recall
from uva.router import Router
from uva.storage.sqlite import Database

TZ = ZoneInfo("America/Sao_Paulo")


def plan(reply="", *actions) -> str:
    return json.dumps({"actions": [{"type": t, "params": p} for t, p in actions], "reply": reply})


@pytest.fixture
def idx_services(services, tmp_path):
    db = Database(tmp_path / "uva.db")
    db.migrate()
    services.vault = Vault(tmp_path / "brain", TZ)
    services.vault.ensure_structure()
    services.index = SearchIndex(db, services.vault)
    services.vault.on_change.append(services.index.note_changed)
    services.vault.on_remove.append(services.index.note_removed)
    services.memory = MemoryService(InMemoryMemoryStore(), services.settings, index=services.index)
    services.goals = InMemoryGoalStore()
    return services


async def test_notes_are_indexed_when_written_and_search_ignores_accents(idx_services):
    vault, index = idx_services.vault, idx_services.index
    vault.create_note("Coração de estudante", "Música do Milton.", folder="recursos")
    hits = await index.search("coracao", kinds=("note",))
    assert [h.ref for h in hits] == ["03-recursos/coracao_de_estudante.md"]
    # Prefix matching: "music" finds "Música"; the title weighs more than the body.
    vault.create_note("Milton", "Cantor; fez coração de estudante.", folder="recursos")
    hits = await index.search("coração", kinds=("note",))
    assert hits[0].ref == "03-recursos/coracao_de_estudante.md" and len(hits) == 2
    assert await index.search("music", kinds=("note",))
    vault.append("Milton", "Mais um parágrafo sobre Travessia.")
    assert [h.ref for h in await index.search("travessia")] == ["03-recursos/milton.md"]


async def test_renamed_note_moves_in_the_index(idx_services):
    vault, index = idx_services.vault, idx_services.index
    vault.create_note("Rascunho", "texto sobre jabuticaba", folder="inbox")
    vault.update_note("Rascunho", title="Jabuticabas")
    refs = {h.ref for h in await index.search("jabuticaba", kinds=("note",))}
    assert "00-inbox/jabuticabas.md" in refs and "00-inbox/rascunho.md" not in refs


async def test_edits_outside_uva_are_picked_up_by_mtime(idx_services):
    vault, index = idx_services.vault, idx_services.index
    note = vault.create_note("Lista", "pão", folder="inbox")
    path = vault.root / note.path
    path.write_text("---\ntitle: Lista\n---\n\nbanana e caqui\n", encoding="utf-8")
    stat = path.stat()
    os.utime(path, (stat.st_atime, stat.st_mtime + 5))  # Obsidian saved it later
    (vault.root / "03-recursos" / "nova.md").write_text("# Nova\n\nbergamota\n", encoding="utf-8")
    assert await index.sync_notes() == {"indexed": 2, "removed": 0}
    assert await index.search("caqui") and await index.search("bergamota")
    assert not await index.search("pão")
    path.unlink()
    assert await index.sync_notes() == {"indexed": 0, "removed": 1}
    assert not await index.search("caqui")


async def test_recall_uses_the_index_not_a_vault_scan(idx_services, monkeypatch):
    vault = idx_services.vault
    vault.create_note("Marmitas frias e requentadas", "Achei as crônicas repetitivas.", folder="recursos")
    await idx_services.memory.save("cidade", "Mora em Florianópolis", relevance=1.0)
    idx_services.settings.memory_working_size = 1
    await idx_services.memory.save("leitura_marmitas", "Largou Marmitas frias na metade", relevance=0.1)
    await idx_services.memory.recompute_working_set()

    def no_scan():
        raise AssertionError("recall must not scan the vault")

    monkeypatch.setattr(vault, "iter_notes", no_scan)
    found = await recall(idx_services, "me fala do livro marmitas frias")
    assert found.notes == ["03-recursos/marmitas_frias_e_requentadas.md"]
    assert "crônicas repetitivas" in found.render()
    assert "Largou Marmitas frias" in found.render()
    # Being shown counts as a use.
    item = await idx_services.memory.get("leitura_marmitas")
    assert item.use_count == 1 and found.memory_keys == ["leitura_marmitas"]


async def test_forgotten_memory_leaves_the_index(idx_services):
    await idx_services.memory.save("gato", "Mingau")
    assert await idx_services.index.search("mingau", kinds=("memory",))
    await idx_services.memory.forget("gato")
    assert not await idx_services.index.search("mingau", kinds=("memory",))


async def test_conversation_log_and_history_search(idx_services):
    provider, _ = with_llm(idx_services, plan("Anotado, Lisboa em maio."), plan("ok"))
    router = Router(idx_services)
    await router.handle_text("vou viajar pra Lisboa em maio")
    turns = await idx_services.index.turns()
    assert [(t.role, t.source) for t in turns] == [("user", "telegram"), ("bot", "telegram")]
    assert turns[0].text == "vou viajar pra Lisboa em maio"

    reply = await router.handle_text("/historico lisboa")
    assert "Conversas com 'lisboa'" in reply.text and "vou viajar pra Lisboa" in reply.text

    action = idx_services.actions.get("history_search")
    result = await action.handler(action.params_model(query="lisboa"), ActionContext(idx_services))
    assert "vou viajar pra Lisboa em maio" in result.text and action.lookup


async def test_sources_are_recorded(idx_services):
    token = turn_source.set("dashboard")
    await idx_services.index.add_exchange("oi", "olá")
    turn_source.reset(token)
    token = notice_source.set("routine")
    await idx_services.index.add_turn("notice", "⏰ rotina rodou")
    notice_source.reset(token)
    turns = await idx_services.index.turns()
    assert [t.source for t in turns] == ["dashboard", "dashboard", "routine"]


async def test_recall_reads_old_conversations_when_asked_about_the_past(idx_services):
    index = idx_services.index
    old = utcnow() - timedelta(days=9)
    await index.add_turn("user", "a reunião com a Carla ficou pra sexta", at=old)
    await index.add_turn("user", "comprei pão", at=old)
    found = await recall(idx_services, "o que eu te falei sobre a Carla semana passada?")
    assert found.turns == 1 and "reunião com a Carla" in found.render()
    # Without a pointer to the past the log is not searched.
    assert (await recall(idx_services, "Carla")).turns == 0


def test_past_window():
    now = utcnow()
    assert past_window("o que eu te disse ontem?", TZ, now)[0]
    past, since, until = past_window("last week", TZ, now)
    assert past and until - since == timedelta(days=14)
    assert past_window("o que eu te falei sobre X", TZ, now) == (True, None, None)
    assert not past_window("me lembra de comprar pão", TZ, now)[0]


async def test_buscar_uses_the_index(idx_services, monkeypatch):
    idx_services.vault.create_note("Receita de pão", "farinha, água e fermento", folder="recursos")
    monkeypatch.setattr(idx_services.vault, "search", lambda *a: (_ for _ in ()).throw(AssertionError))
    reply = await Router(idx_services).handle_text("/buscar fermento")
    assert "03-recursos/receita_de_pao.md" in reply.text


async def test_reindexar_rebuilds_everything(idx_services):
    idx_services.vault.create_note("Nota", "conteúdo", folder="inbox")
    await idx_services.memory.save("cor", "azul")
    await idx_services.goals.add(Goal(id="g1", title="Acompanhar o Avaí", results=[GoalResult(summary="ganhou")]))
    await idx_services.index.add_turn("user", "olá")
    reply = await Router(idx_services).handle_text("/reindexar")
    assert "1 notas, 1 memórias, 1 objetivos, 1 falas" in reply.text
    assert [h.ref for h in await idx_services.index.search("avai", kinds=("goal",))] == ["g1"]


async def test_memoria_key_counts_as_a_use(idx_services):
    await idx_services.memory.save("cidade", "Floripa")
    await Router(idx_services).handle_text("/memoria cidade")
    assert (await idx_services.memory.get("cidade")).use_count == 1


class SharedVersion:
    """Stand-in for the Redis counter, shared by two "processes"."""

    value = 0

    async def get(self) -> int:
        return SharedVersion.value

    async def bump(self) -> int:
        SharedVersion.value += 1
        return SharedVersion.value


async def test_working_set_cache_is_invalidated_across_processes():
    settings = make_settings()
    store = InMemoryMemoryStore()
    bot = MemoryService(store, settings, version=SharedVersion())
    worker = MemoryService(store, settings, version=SharedVersion())
    await bot.save("cidade", "Floripa")
    assert [i.key for i in await bot.working_memory()] == ["cidade"]
    await worker.save("time", "Avaí")  # e.g. a goal run in the worker
    assert {i.key for i in await bot.working_memory()} == {"cidade", "time"}


async def test_without_a_version_the_cache_still_works():
    store = InMemoryMemoryStore()
    service = MemoryService(store, make_settings())
    await service.save("a", "1")
    assert [i.key for i in await service.working_memory()] == ["a"]
