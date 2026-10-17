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
    assert note.path == "03-recursos/ideia_cafe_cha.md"
    text = (vault.root / note.path).read_text(encoding="utf-8")
    meta, body = parse_note(text)
    assert meta == {
        "title": "Ideia café chá",
        "created": "2026-10-11T12:30:00-03:00",
        "tags": ["bebidas"],
        "source": "uva-bot",
    }
    assert "[[cafe|Café]]" in body and body.startswith("# Ideia café chá")


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
    assert note.path == "03-recursos/pesquisa_formula_1.md"
    body = vault.read(note.path).body
    assert "## Fontes" in body and "[F1](https://f1)" in body
    assert "[[pesquisa_formula_1|Pesquisa - Fórmula 1]]" in next((vault.root / "diario").iterdir()).read_text(encoding="utf-8")


async def test_nota_and_buscar_commands(services, vault):
    services.vault = vault
    router = Router(services)
    reply = await router.handle_text("/nota comprar ração do gato amanhã cedo antes do trabalho")
    assert reply.text.startswith("📝 Nota salva: 00-inbox/comprar_racao_do_gato")
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
    assert "📝 Salvo em 03-recursos/pesquisa_f1.md" in reply.text
    assert "Resumo da F1." in vault.read("Pesquisa - F1").body


async def test_llm_can_create_notes(services, vault):
    services.vault = vault
    with_llm(
        services,
        '{"actions": [{"type": "note_create", "params": {"title": "Viagem", "body": "Ir a Floripa", "folder": "projetos"}}], "reply": "Anotado."}',
    )
    reply = await Router(services).handle_text("anota que quero viajar pra Floripa")
    assert "01-projetos/viagem.md" in reply.text


def test_file_names_are_portuguese_snake_case():
    from uva.brain.naming import file_stem, looks_portuguese

    assert file_stem("Marmitas Frias e Esquentadas") == "marmitas_frias_e_esquentadas"
    assert file_stem("Previsão do tempo: Mafra (amanhã)") == "previsao_do_tempo_mafra_amanha"
    assert looks_portuguese("Previsão do tempo em Mafra")
    assert not looks_portuguese("Mafra weather and Tuesday commute conditions")
    assert not looks_portuguese("Pesquisa - Prêmio Jabuti 2026 latest updates")
    assert looks_portuguese("Pesquisa - Prêmio Jabuti 2026 últimas notícias")


async def test_english_title_gets_a_portuguese_one(services, tmp_path):
    import json

    from fakes import with_llm

    from uva.router import Router

    services.vault = Vault(tmp_path / "b", ZoneInfo("America/Sao_Paulo"))
    services.vault.ensure_structure()
    plan = {"actions": [{"type": "note_create", "params": {"title_pt": "Terrine food what is it", "body": "Terrine é um patê."}}], "reply": ""}
    with_llm(services, json.dumps(plan), "Terrine: o que é")
    reply = (await Router(services).handle_text("anota o que é terrine")).text
    assert "00-inbox/terrine_o_que_e.md" in reply
    note = services.vault.read("terrine_o_que_e")
    assert note.title == "Terrine o que é"  # ":" is dropped from titles


async def test_rename_existing_notes_and_links_then_revert(services, tmp_path):
    import subprocess

    from fakes import with_llm

    from uva.bootstrap import _init_brain_git
    from uva.selfmod.git import GitRepo

    root = tmp_path / "brain"
    vault = Vault(root, ZoneInfo("America/Sao_Paulo"))
    vault.ensure_structure()
    old = root / "03-recursos" / "Pesquisa - Mafra weather and Tuesday commute conditions.md"
    old.write_text("---\ntitle: Pesquisa - Mafra weather and Tuesday commute conditions\n---\n\n# Pesquisa - Mafra weather and Tuesday commute conditions\n\nChuva.\n", encoding="utf-8")
    book = root / "03-recursos" / "Marmitas frias e requentadas.md"
    book.write_text("---\ntitle: Marmitas frias e requentadas\n---\n\n![[05-anexos/Capa Livro.jpg]]\nVer [[Pesquisa - Mafra weather and Tuesday commute conditions]].\n", encoding="utf-8")
    (root / "05-anexos" / "Capa Livro.jpg").write_bytes(b"x")
    _init_brain_git(vault)
    services.vault = vault
    services.brain_repo = GitRepo(root, "kundlatsch", "k@example.com")
    with_llm(services, "Clima em Mafra e trânsito de terça")  # the prefix is kept

    text = (await Router(services).handle_text("/brain renomear")).text
    assert "03-recursos/marmitas_frias_e_requentadas.md" in text
    assert "03-recursos/pesquisa_clima_em_mafra_e_transito_de_terca.md" in text
    new_book = (root / "03-recursos" / "marmitas_frias_e_requentadas.md").read_text(encoding="utf-8")
    assert "[[pesquisa_clima_em_mafra_e_transito_de_terca|Pesquisa - Clima em Mafra e trânsito de terça]]" in new_book
    assert "![[05-anexos/capa_livro.jpg]]" in new_book and (root / "05-anexos" / "capa_livro.jpg").exists()
    assert not old.exists()

    reverted = (await Router(services).handle_text("/reverter brain")).text
    assert reverted.startswith("↩️ Second brain: desfeito")
    assert old.exists() and book.exists()
    log = subprocess.run(["git", "-C", str(root), "log", "--format=%s"], capture_output=True, text=True, encoding="utf-8").stdout
    assert "Renomeia 2 nota(s)" in log


async def test_note_actions_in_english(services, vault):
    from uva import i18n
    from uva.actions import ActionContext

    i18n.set_language("en")
    ctx = ActionContext(services)
    read = services.actions.get("note_read")
    assert (await read.handler(read.params_model(note="x"), ctx)).text == "The second brain isn't configured."
    services.vault = vault
    result = await read.handler(read.params_model(note="nada aqui"), ctx)
    assert result.text == "Couldn't find the note 'nada aqui'."
    found = Research("F1", "web", "web", "Summary.", [SearchResult("F1", "https://f1")])
    assert "Sources:\n- F1: https://f1" in found.render()
