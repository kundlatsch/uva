import json
from zoneinfo import ZoneInfo

import pytest
from fakes import InMemoryGoalStore, InMemoryMemoryStore, with_llm

from uva.brain.vault import Vault
from uva.goals.store import Goal, GoalResult, GoalStatus
from uva.memory import MemoryService
from uva.recall import recall, terms
from uva.router import Router

BOOK = """Li até a metade e desisti. Achei as crônicas repetitivas, mas gostei do humor.
Nota: 2/5."""


def plan(reply="", *actions) -> str:
    return json.dumps({"actions": [{"type": t, "params": p} for t, p in actions], "reply": reply})


@pytest.fixture
def mem_services(services, tmp_path):
    services.settings.memory_working_size = 1
    services.memory = MemoryService(InMemoryMemoryStore(), services.settings)
    services.vault = Vault(tmp_path / "brain", ZoneInfo("America/Sao_Paulo"))
    services.vault.ensure_structure()
    services.goals = InMemoryGoalStore()
    return services


async def seed(services):
    await services.memory.save("cidade", "Mora em Florianópolis", relevance=1.0)
    await services.memory.save("leitura_marmitas", "Largou Marmitas frias e requentadas na metade", relevance=0.2)
    services.vault.create_note("Marmitas frias e requentadas", BOOK, folder="recursos", subfolder="Livros que já li")
    services.vault.create_note("Receita de marmita fit", "frango e batata doce", folder="recursos")
    await services.goals.add(Goal(
        title="Conferir dados do Ricardo Terto", status=GoalStatus.CONCLUIDO,
        description="autor de Marmitas frias e requentadas",
        results=[GoalResult(summary="Semifinalista do Jabuti 2024 em Crônica", note="01-projetos/Objetivo.md")],
    ))


def test_terms_drop_filler_words():
    assert terms("me fala sobre o livro Marmitas frias e requentadas que eu li") == [
        "marmitas", "frias", "requentadas"
    ]


async def test_recall_searches_every_system(mem_services):
    await seed(mem_services)
    found = await recall(mem_services, "me fala sobre o livro Marmitas frias e requentadas que eu li")
    text = found.render()
    assert "Largou Marmitas frias" in text  # long-term memory outside the working set
    assert "Mora em Florianópolis" not in text  # already in the prompt (working set)
    assert "crônicas repetitivas" in text  # the note's content, not just its name
    assert found.notes == ["03-recursos/Livros que já li/marmitas_frias_e_requentadas.md"]
    assert "Receita de marmita" not in text  # one shared word isn't a match
    assert "Semifinalista do Jabuti" in text  # what a goal found


async def test_recall_skips_archived_versions(mem_services):
    mem_services.vault.create_note("Marmitas frias e requentadas 2026-10-11", "versão velha", folder="arquivo")
    found = await recall(mem_services, "marmitas frias requentadas")
    assert found.notes == []


async def test_question_is_answered_with_recalled_material_in_one_call(mem_services):
    await seed(mem_services)
    provider, _ = with_llm(mem_services, plan("Você largou na metade; achou repetitivo, nota 2/5."))
    reply = await Router(mem_services).handle_text("me fala sobre o livro Marmitas frias e requentadas que eu li")
    assert reply.text == "Você largou na metade; achou repetitivo, nota 2/5."
    assert len(provider.calls) == 1
    system = provider.calls[0]["system"]
    assert "Found in your systems" in system and "crônicas repetitivas" in system


async def test_lookup_action_is_read_and_answered_in_the_same_turn(mem_services):
    await seed(mem_services)
    provider, _ = with_llm(
        mem_services,
        plan("Vou abrir a nota.", ("note_read", {"note": "Receita de marmita fit"})),
        plan("Sua receita leva frango e batata doce."),
    )
    reply = await Router(mem_services).handle_text("qual era a minha receita de marmita fit?")
    assert reply.text == "Sua receita leva frango e batata doce."  # no "Vou abrir a nota."
    assert len(provider.calls) == 2
    fed_back = provider.calls[1]["messages"][-1].content
    assert "frango e batata doce" in fed_back and "o usuário ainda não viu" in fed_back


async def test_lookup_rounds_are_capped(mem_services):
    await seed(mem_services)
    looping = plan("", ("recall", {"query": "marmita"}))
    provider, _ = with_llm(mem_services, looping, looping, looping)
    reply = await Router(mem_services).handle_text("procura marmita em tudo")
    assert len(provider.calls) == 3  # 1 + MAX_LOOKUP_ROUNDS, then the last lookup is shown
    assert "Receita de marmita fit" in reply.text


async def test_side_effects_are_not_fed_back(mem_services):
    provider, _ = with_llm(mem_services, plan("Anotado.", ("note_create", {"title": "Ideia", "body": "x"})))
    reply = await Router(mem_services).handle_text("anota uma ideia: x")
    assert len(provider.calls) == 1 and "📝 Nota salva" in reply.text
