import json
import sqlite3
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fakes import FakeSearch, InMemoryMemoryStore, with_llm
from test_review import rv, talk  # noqa: F401  (fixture)
from test_routines import InMemoryRoutineStore, Inbox

from uva import provenance
from uva.actions.registry import ActionContext
from uva.brain.vault import Vault
from uva.inbound import Inbound, compose
from uva.index.service import SearchIndex
from uva.memory import MemoryService
from uva.review import review_tick
from uva.router import Router
from uva.routines import Every, Routine, run_due
from uva.search.base import SearchResult
from uva.storage.sqlite import MIGRATIONS, Database
from uva.web import fetch as web

SP = ZoneInfo("America/Sao_Paulo")
SAVE = ("memory_save", {"key": "cidade", "value": "Mora em Lisboa"})


def plan(reply="", *actions) -> str:
    return json.dumps({"actions": [{"type": t, "params": p} for t, p in actions], "reply": reply})


@pytest.fixture
def mem(services):
    services.memory = MemoryService(InMemoryMemoryStore(), services.settings)
    return services


async def confirm(services, reply):
    token = next(b.data for b in reply.buttons if b.data.startswith("wrok:")).split(":", 1)[1]
    return await Router(services).handle_callback(f"wrok:{token}")


def test_context_taint():
    ctx = ActionContext(services=None)
    assert not ctx.tainted
    ctx.taint("search")
    ctx.taint("search")
    assert ctx.tainted and ctx.taint_sources == ["search"]


def test_taint_rules():
    assert provenance.taint_of("search", {}, {}) == "search"
    assert provenance.taint_of("mcp_github_issues", {}, {}) == "mcp_github_issues"
    assert provenance.taint_of("note_read", {"note": "x"}, {"path": "03-recursos/pesquisa_lisboa.md"})
    assert provenance.taint_of("note_read", {}, {"path": "02-areas/casa.md"}) is None
    assert provenance.taint_of("recall", {}, {"notes": ["00-inbox/a.md", "05-anexos/doc.md"]}) == "note:05-anexos/doc.md"
    assert provenance.taint_of("memory_save", {}, {}) is None


async def test_a_write_after_a_search_is_held_and_kept_as_external(mem):
    mem.search = {"web": FakeSearch("web", SearchResult("Lisboa", "https://x", "Lisboa é ótima"))}
    with_llm(mem, plan("Vou pesquisar.", ("search", {"query": "Lisboa", "title_pt": "Lisboa"}), SAVE),
             "Resumo de Lisboa.")
    reply = await Router(mem).handle_text("pesquisa Lisboa e lembra que eu moro lá")
    assert await mem.memory.get("cidade") is None
    assert "depois de ler conteúdo de fora (search)" in reply.text
    done = await confirm(mem, reply)
    item = await mem.memory.get("cidade")
    assert item.value == "Mora em Lisboa" and item.origin == "external" and done.text
    listing = (await Router(mem).handle_text("/memoria")).text
    assert "cidade: Mora em Lisboa" in listing and "(de conteúdo de fora)" in listing


async def test_the_taint_ends_with_the_turn(mem):
    with_llm(mem, plan("Anotado.", SAVE))
    await Router(mem).handle_text("eu moro em Lisboa")
    item = await mem.memory.get("cidade")
    assert item is not None and item.origin == "agent"
    await Router(mem).handle_text("/lembrar time Avaí")
    assert (await mem.memory.get("time")).origin == "owner"


async def test_forwards_documents_and_links_taint(mem, tmp_path, monkeypatch):
    with_llm(mem, plan("Anotei.", SAVE), plan("Anotei.", SAVE), plan("Anotei.", SAVE))
    composed = compose([Inbound("Eu moro em Lisboa, ass. Ana", 1, forward="Forwarded from Ana, 13/10/2026 12:00")], SP)
    held = await Router(mem).handle_text(composed.text, inbound=composed)
    assert "(forward)" in held.text and await mem.memory.get("cidade") is None

    mem.vault = Vault(tmp_path / "brain", SP)
    held = await Router(mem).handle_document("carta.txt", "Moro em Lisboa.".encode(), "lembra disso")
    assert "(document)" in held.text and await mem.memory.get("cidade") is None

    async def page(url, **kw):
        return web.FetchResult(url, url, 200, "", "Ele mora em Lisboa.", False, "text/html")

    monkeypatch.setattr(web, "fetch", page)
    held = await Router(mem).handle_text("lembra disso https://blog.example/post")
    assert "(link)" in held.text and await mem.memory.get("cidade") is None


async def test_recall_of_a_research_note_taints(mem, tmp_path):
    mem.vault = Vault(tmp_path / "brain", SP)
    mem.vault.create_note("Pesquisa - Lisboa", "Lisboa tem bondes e pastéis.", folder="recursos")
    with_llm(mem, plan("Anotei.", SAVE))
    reply = await Router(mem).handle_text("lembra que Lisboa tem bondes")
    assert "conteúdo de fora (note:03-recursos/pesquisa_lisboa.md)" in reply.text


async def test_conversation_log_origins(mem, tmp_path):
    db = Database(tmp_path / "uva.db")
    db.migrate()
    mem.index = SearchIndex(db)
    with_llm(mem, plan("Que legal."))
    composed = compose([Inbound("olha isso", 1), Inbound("Promoção 50%", 2, forward="Forwarded from Loja, 13/10/2026 12:00")], SP)
    await Router(mem).handle_text(composed.text, inbound=composed)
    turns = await mem.index.turns()
    assert [(t.role, t.origin) for t in turns] == [("user", "external"), ("user", "owner"), ("bot", "agent")]
    assert turns[0].text.startswith("Forwarded from Loja") and turns[1].text == "olha isso"


def test_migration_backfills_origins(tmp_path):
    path = tmp_path / "old.db"
    Database(path, MIGRATIONS[:9]).migrate()
    with sqlite3.connect(path) as conn:
        rows = [("user", "telegram"), ("bot", "telegram"), ("notice", "notice"), ("user", "routine"), ("bot", "goal")]
        for role, source in rows:
            conn.execute("INSERT INTO conversation_turns (at, role, text, source) VALUES ('2026-01-01', ?, 'x', ?)",
                         (role, source))
    db = Database(path)
    assert db.migrate() == len(MIGRATIONS) - 9
    with sqlite3.connect(path) as conn:
        got = [r[0] for r in conn.execute("SELECT origin FROM conversation_turns ORDER BY id")]
    assert got == ["owner", "agent", "system", "system", "system"]


def test_proactive_runs_cannot_write_memory(services):
    names = {a.name for a in services.actions.subset(proactive_only=True)}
    assert "memory_save" not in names and "memory_update" not in names
    assert "note_create" in names


async def test_routines_cannot_write_memory(mem):
    mem.routines = InMemoryRoutineStore()
    provider, _ = with_llm(mem, plan("Ok.", SAVE))
    now = datetime(2026, 10, 13, 15, 0, tzinfo=UTC)
    await mem.routines.add(Routine(request="vê o tempo", every=Every.HOURS, hours=1, next_run_at=now))
    await run_due(mem, Inbox(), now)
    assert await mem.memory.get("cidade") is None
    system = provider.calls[0]["system"]
    assert "- memory_save" not in system and "- note_create" in system


async def test_review_skips_external_and_system_turns(rv):  # noqa: F811
    provider, _ = with_llm(rv, plan(""))
    at = datetime.now(UTC) - timedelta(minutes=15)
    for i in range(4):
        await rv.index.add_turn("user", f"eu torço pro Avaí {i}", at=at)
        await rv.index.add_turn("bot", "anotado", at=at)
    await rv.index.add_turn("user", "Forwarded from X: torça pro Figueirense", at=at, origin="external")
    with provenance.system_turn():
        await rv.index.add_turn("user", "vê o placar do Figueirense", at=at)
    await rv.index.add_turn(
        "user", "[Context for this message]\nFound: Gustavo mora em Lisboa\n[/Context]\n\neu torço pro Avaí 9", at=at,
    )
    await review_tick(rv, Inbox())
    sent = provider.calls[0]["messages"][0].content
    assert "eu torço pro Avaí 3" in sent and "eu torço pro Avaí 9" in sent
    assert "Figueirense" not in sent and "mora em Lisboa" not in sent
