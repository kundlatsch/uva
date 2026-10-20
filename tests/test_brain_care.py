import os
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from uva.actions.registry import ActionContext
from uva.brain import lint
from uva.brain.related import refresh_related
from uva.brain.vault import MANAGED, Vault, strip_managed
from uva.index.service import SearchIndex
from uva.storage.sqlite import Database

SP = ZoneInfo("America/Sao_Paulo")
HIS = "# Viagem\n\nMinhas ideias para Lisboa.\n- pastéis\n"


@pytest.fixture
def bs(services, tmp_path):
    services.vault = Vault(tmp_path / "brain", SP)
    services.vault.ensure_structure()
    return services


def his_note(vault, name="viagem.md", text=HIS):
    path = vault.root / "02-areas" / name
    path.write_text(text, encoding="utf-8")  # written in Obsidian: no front matter
    return f"02-areas/{name}"


async def run(services, name, **params):
    action = services.actions.get(name)
    return await action.handler(action.params_model(**params), ActionContext(services))


async def test_appends_to_his_note_go_in_managed_blocks(bs):
    vault = bs.vault
    path = his_note(vault)
    await run(bs, "note_append", note=path, text="Sugestão: Belém.")
    body = vault.read(path).body
    assert MANAGED.search(body) and "Sugestão: Belém." in body and "Minhas ideias para Lisboa." in body
    mine = vault.create_note("Lista", "a", folder="inbox")
    await run(bs, "note_append", note=mine.path, text="b")
    assert "uva:begin" not in vault.read(mine.path).body  # his only: UVA's own notes are plain


async def test_update_changes_only_uvas_blocks_unless_full_rewrite(bs):
    vault = bs.vault
    path = his_note(vault)
    await run(bs, "note_append", note=path, text="Sugestão: Belém.")
    await run(bs, "note_read", note=path)
    # The LLM sends the full content back (his text + its new part): only the block changes.
    full = "Minhas ideias para Lisboa.\n- pastéis\n\nSugestão: Belém e Sintra."
    result = await run(bs, "note_update", note=path, body=full)
    assert result.data.get("path") == path
    body = vault.read(path).body
    assert strip_managed(body).endswith("Minhas ideias para Lisboa.\n- pastéis")
    assert len(MANAGED.findall(body)) == 1 and "Belém e Sintra" in MANAGED.search(body).group(0)
    assert body.count("Minhas ideias para Lisboa.") == 1
    assert "source" not in (vault.root / path).read_text(encoding="utf-8")  # still his note
    await run(bs, "note_read", note=path)
    await run(bs, "note_update", note=path, body="Tudo novo.", full_rewrite=True)
    assert vault.read(path).body.strip().endswith("Tudo novo.") and "pastéis" not in vault.read(path).body


async def test_a_note_edited_after_reading_is_not_overwritten(bs):
    vault = bs.vault
    path = his_note(vault)
    await run(bs, "note_read", note=path)
    file = vault.root / path
    file.write_text(HIS + "- linha nova no Obsidian\n", encoding="utf-8")
    stamp = file.stat().st_mtime + 5
    os.utime(file, (stamp, stamp))
    refused = await run(bs, "note_update", note=path, body="x")
    assert refused.data == {"ok": False, "stale": path} and "mudou depois que eu li" in refused.text
    assert "linha nova no Obsidian" in file.read_text(encoding="utf-8")
    again = await run(bs, "note_append", note=path, text="y")
    assert again.data.get("stale") == path  # still refused until it is read again
    await run(bs, "note_read", note=path)
    ok = await run(bs, "note_append", note=path, text="y")
    assert ok.data.get("path") == path


async def test_related_links(bs, tmp_path):
    db = Database(tmp_path / "uva.db")
    db.migrate()
    bs.index = SearchIndex(db, bs.vault)
    bs.vault.on_change.append(bs.index.note_changed)
    vault = bs.vault
    for title in ("Lisboa bairros", "Lisboa restaurantes", "Porto vinhos Lisboa", "Receita de bolo"):
        vault.create_note(title, f"Notas sobre {title.lower()} e viagem a Lisboa.", folder="recursos")
    note = vault.create_note("Pesquisa - Lisboa", "Lisboa: bairros, restaurantes, viagem.", folder="recursos")
    links = await refresh_related(bs, note.path)
    assert 1 <= len(links) <= 3 and all("pesquisa_lisboa" not in link for link in links)
    body = vault.read(note.path).body
    assert body.rstrip().splitlines()[-len(links) - 2] == "## Relacionadas"
    await refresh_related(bs, note.path)  # refreshed in place, never twice
    assert vault.read(note.path).body.count("## Relacionadas") == 1


def test_lint_findings_on_a_fixture_vault(bs):
    vault = bs.vault
    now = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
    vault.create_note("Índice", "Ver [[lisboa]] e [[nao_existe]].", folder="areas", now=now)
    vault.create_note("Lisboa", "Cidade.", folder="recursos", now=now)
    vault.create_note("Solta", "Sem links.", folder="recursos", now=now)
    vault.create_note("Pesquisa - Velha", "x", folder="recursos", tags=["pesquisa"], now=now - timedelta(days=200))
    vault.create_note("Receitas de bolo", "a [[lisboa]]", folder="inbox", now=now)
    vault.create_note("Receitas de bolos", "b [[lisboa]]", folder="inbox", now=now)
    found = lint.scan(vault, now)
    assert found.broken == [("02-areas/indice.md", "nao_existe")]
    assert "03-recursos/solta.md" in found.orphans and "03-recursos/lisboa.md" not in found.orphans
    assert found.old_research == ["03-recursos/pesquisa_velha.md"]
    assert found.duplicates == [("00-inbox/receitas_de_bolo.md", "00-inbox/receitas_de_bolos.md")]


async def test_lint_writes_a_report_and_one_line(bs):
    bs.vault.create_note("Índice", "Ver [[nao_existe]].", folder="areas")
    found, line = await lint.brain_lint(bs)
    assert "1 links quebrados" in line and "00-inbox/revisao_do_cerebro_" in line
    report = bs.vault.read(line.split("detalhes em ")[1].rstrip(").")).body
    assert "## Links quebrados (1)" in report and "`[[nao_existe]]`" in report
    found, _ = await lint.brain_lint(bs)  # the reports don't count as duplicates of each other
    assert found.duplicates == []
