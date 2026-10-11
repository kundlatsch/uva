from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from fakes import FakeSearch, with_llm

from uva.actions.search import Research, save_research
from uva.brain.vault import FOLDERS, README_NAME, NoteNotFoundError, Vault, parse_note
from uva.router import Router
from uva.search.base import SearchResult

TZ = ZoneInfo("America/Sao_Paulo")
NOW = datetime(2026, 10, 11, 15, 30, tzinfo=UTC)


@pytest.fixture
def vault(tmp_path) -> Vault:
    v = Vault(tmp_path / "brain", TZ)
    v.ensure_structure()
    return v


def test_structure_is_created_once(tmp_path):
    v = Vault(tmp_path / "brain", TZ)
    assert v.ensure_structure() is True
    assert v.ensure_structure() is False
    for folder in FOLDERS.values():
        assert (v.root / folder).is_dir()
    assert "PARA" in (v.root / README_NAME).read_text(encoding="utf-8")


def test_note_has_front_matter_and_links(vault):
    note = vault.create_note(
        "Ideia: café/chá?", "Corpo", folder="recursos", tags=["bebidas"], links=["Café"], now=NOW
    )
    assert note.path == "03-recursos/Ideia café chá.md"
    text = (vault.root / note.path).read_text(encoding="utf-8")
    meta, body = parse_note(text)
    assert meta == {
        "title": "Ideia café chá",
        "created": "2026-10-11T12:30:00-03:00",
        "tags": ["bebidas"],
        "source": "uva-bot",
    }
    assert "[[Café]]" in body and body.startswith("# Ideia café chá")


def test_existing_notes_are_never_overwritten(vault):
    first = vault.create_note("Mesmo", "um")
    second = vault.create_note("Mesmo", "dois")
    assert first.path != second.path
    assert vault.read(first.path).body.endswith("um\n")


def test_append_and_read_by_title(vault):
    vault.create_note("Lista de compras", "- pão")
    vault.append("lista de compras", "- leite", now=NOW)
    body = vault.read("Lista de compras").body
    assert "- pão" in body and "### 11/10/2026 12:30" in body and "- leite" in body


def test_daily_note(vault):
    vault.append_daily("primeira", now=NOW)
    note = vault.append_daily("segunda", now=NOW)
    assert note.path == "diario/2026-10-11.md"
    assert "- 12:30 primeira" in note.body and "- 12:30 segunda" in note.body


def test_search_is_accent_insensitive_and_ranks_titles(vault):
    vault.create_note("Receita de pão", "farinha")
    vault.create_note("Mercado", "comprar pao e café")
    hits = vault.search("PÃO")
    assert [h.title for h in hits] == ["Receita de pão", "Mercado"]


def test_paths_outside_the_vault_are_refused(vault):
    with pytest.raises(NoteNotFoundError):
        vault.read("../../etc/passwd")


def test_research_note(vault):
    found = Research("Fórmula 1", "wikipedia", "wikipedia", "Resumo.", [SearchResult("F1", "https://f1")])
    note = save_research(vault, found)
    assert note.path == "03-recursos/Pesquisa - Fórmula 1.md"
    body = vault.read(note.path).body
    assert "## Fontes" in body and "[F1](https://f1)" in body
    assert "[[Pesquisa - Fórmula 1]]" in next((vault.root / "diario").iterdir()).read_text(encoding="utf-8")


async def test_nota_and_buscar_commands(services, vault):
    services.vault = vault
    router = Router(services)
    reply = await router.handle_text("/nota comprar ração do gato amanhã cedo antes do trabalho")
    assert reply.text.startswith("📝 Nota salva: 00-inbox/comprar ração do gato")
    found = (await router.handle_text("/buscar racao")).text
    assert "00-inbox/" in found


async def test_search_result_becomes_a_note(services, vault):
    services.vault = vault
    services.search = {"web": FakeSearch("web", SearchResult("F1", "https://f1.com", "texto"))}
    with_llm(
        services,
        '{"actions": [{"type": "search", "params": {"query": "F1"}}], "reply": ""}',
        "Resumo da F1.",
    )
    reply = await Router(services).handle_text("pesquisa F1")
    assert "📝 Salvo em 03-recursos/Pesquisa - F1.md" in reply.text
    assert "Resumo da F1." in vault.read("Pesquisa - F1").body


async def test_llm_can_create_notes(services, vault):
    services.vault = vault
    with_llm(
        services,
        '{"actions": [{"type": "note_create", "params": {"title": "Viagem", "body": "Ir a Floripa", "folder": "projetos"}}], "reply": "Anotado."}',
    )
    reply = await Router(services).handle_text("anota que quero viajar pra Floripa")
    assert "01-projetos/Viagem.md" in reply.text
