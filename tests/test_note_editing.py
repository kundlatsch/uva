import json
from zoneinfo import ZoneInfo

import pytest
from fakes import FakeBusy, InMemoryUsageStore, with_llm
from test_stm import InMemorySessionBackend

from uva.brain.vault import NoteNotFoundError, PlaceholderError, Vault, parse_note
from uva.llm.budget import Budget, Scope, UsageRecord
from uva.router import Router
from uva.stm.service import ShortTermMemory

PLACEHOLDER = "- <texto anterior mantido aqui se houver mais conteúdo na nota>"


@pytest.fixture
def vault(tmp_path) -> Vault:
    v = Vault(tmp_path / "brain", ZoneInfo("America/Sao_Paulo"))
    v.ensure_structure()
    return v


def test_body_with_its_own_h1_is_not_duplicated(vault):
    note = vault.create_note("Livro", "# Livro — Autor\n\ntexto", folder="recursos")
    raw = (vault.root / note.path).read_text(encoding="utf-8")
    assert raw.count("\n# ") == 1 and "# Livro — Autor" in raw


@pytest.mark.parametrize(
    "text",
    [PLACEHOLDER, "<conteúdo original da nota>", "[... restante da nota]", "<resto do texto aqui>"],
)
def test_placeholders_are_refused_everywhere(vault, text):
    with pytest.raises(PlaceholderError):
        vault.create_note("X", f"algo\n{text}")
    note = vault.create_note("Y", "ok")
    with pytest.raises(PlaceholderError):
        vault.append(note.path, text)
    with pytest.raises(PlaceholderError):
        vault.update_note(note.path, body=text)


def test_update_renames_keeps_created_and_backs_up(vault):
    original = vault.create_note("Marmitas fiz esquentadas", "- opinião A", folder="recursos",
                                 subfolder="Livros", tags=["livros"])
    created = original.created
    note, backup = vault.update_note(original.path, title="Marmitas frias e requentadas")

    assert note.path == "03-recursos/Livros/Marmitas frias e requentadas.md"
    assert not (vault.root / original.path).exists()  # renamed, not duplicated
    assert note.created == created and note.tags == ["livros"] and "- opinião A" in note.body
    assert note.body.lstrip().startswith("# Marmitas frias e requentadas")  # heading follows the title
    assert backup.startswith("04-arquivo/Versões/Marmitas fiz esquentadas ")
    meta, old_body = parse_note((vault.root / backup).read_text(encoding="utf-8"))
    assert meta["title"] == "Marmitas fiz esquentadas" and "- opinião A" in old_body


def test_update_body_replaces_content(vault):
    note = vault.create_note("Nota", "velho")
    updated, _ = vault.update_note(note.path, body="novo conteúdo completo")
    assert "novo conteúdo completo" in updated.body and "velho" not in updated.body


def test_partial_title_resolves_only_when_unambiguous(vault):
    vault.create_note("Marmitas frias e requentadas", "x", folder="recursos")
    assert vault.read("marmitas").title == "Marmitas frias e requentadas"
    vault.create_note("Marmitas da semana", "y")
    with pytest.raises(NoteNotFoundError):
        vault.read("marmitas")


async def test_rename_request_goes_through_note_update(services, vault):
    services.vault = vault
    vault.create_note("Marmitas fiz esquentadas", "- opinião", folder="recursos", subfolder="Livros que já li")
    plan = {
        "actions": [{"type": "note_update", "params": {"note": "Marmitas fiz esquentadas",
                                                       "title": "Marmitas frias e requentadas"}}],
        "reply": "Corrigido.",
    }
    with_llm(services, json.dumps(plan))
    reply = await Router(services).handle_text('corrija o título para "Marmitas frias e requentadas"')
    assert "✏️ Nota atualizada: 03-recursos/Livros que já li/Marmitas frias e requentadas.md" in reply.text
    files = sorted(p.name for p in (vault.root / "03-recursos/Livros que já li").iterdir())
    assert files == ["Marmitas frias e requentadas.md"]


async def test_llm_placeholder_write_is_refused(services, vault):
    services.vault = vault
    plan = {"actions": [{"type": "note_create", "params": {"title": "Z", "body": PLACEHOLDER}}], "reply": ""}
    with_llm(services, json.dumps(plan))
    reply = await Router(services).handle_text("cria a nota")
    assert "Não criei a nota" in reply.text and not list((vault.root / "00-inbox").glob("Z*"))


async def test_long_dictation_survives_in_short_term_memory(services):
    services.busy = FakeBusy()
    services.stm = ShortTermMemory(InMemorySessionBackend(), services.settings)
    await services.stm.add_exchange("[voz] " + "palavra " * 500, "ok")
    turn = (await services.stm.session()).turns[0]
    assert len(turn.text) > 3000  # the old 500-char cut made the LLM invent the rest


async def test_blocked_voice_message_says_transcript_was_kept(services):
    from test_audio import FakeTranscriptions, transcriber

    with_llm(services)  # (sets its own budget, so the blocking one goes after)
    usage = InMemoryUsageStore()
    services.budget = Budget(usage, services.settings, services.tz)
    usage.records.append(UsageRecord(provider="p", model="m", input_tokens=0, output_tokens=0, purpose="t",
                                     cost_usd=services.settings.llm_daily_budget_usd, scope=Scope.INTERACTIVE))
    services.transcriber = transcriber(FakeTranscriptions(text="anota tudo isso no brain"), None)
    # Transcription itself is allowed only within budget; simulate it passing.
    services.budget.check = _allow_once(services.budget.check)
    _, reply = await Router(services).handle_audio(b"x", seconds=5)
    assert "guardei a transcrição inteira" in reply.text


def _allow_once(check):
    calls = {"n": 0}

    async def wrapper(scope, now=None):
        calls["n"] += 1
        if calls["n"] == 1:
            from uva.llm.budget import BudgetStatus

            return BudgetStatus(True)
        return await check(scope, now)

    return wrapper
