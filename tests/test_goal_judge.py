import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fakes import FakeBusy, InMemoryGoalStore, with_llm
from test_routines import Inbox

from uva.brain.vault import Vault
from uva.goals.runner import adapted_interval, proactive_tick, result_hash
from uva.goals.store import Goal, GoalKind, GoalStatus
from uva.router import Router

NOW = datetime(2026, 10, 11, 15, 0, tzinfo=UTC)


def step(reply="", done=True) -> str:
    return json.dumps({"actions": [], "reply": reply, "done": done})


def verdict(value, reason="") -> str:
    return json.dumps({"verdict": value, "reason": reason})


@pytest.fixture
def goals(services, tmp_path):
    services.goals = InMemoryGoalStore()
    services.busy = FakeBusy()
    services.vault = Vault(tmp_path / "brain", ZoneInfo("America/Sao_Paulo"))
    services.vault.ensure_structure()
    return services.goals


def goal(**kw) -> Goal:
    return Goal(**{"title": "Achar o livro do Terto no Jabuti", "kind": GoalKind.UNICO,
                   "next_run_at": NOW - timedelta(minutes=1), **kw})


async def test_judge_must_agree_before_completing(services, goals):
    provider, _ = with_llm(services, step("Achei: foi semifinalista com X.", True),
                           verdict("continue", "Falta a fonte oficial."))
    g = goal(verify="a nota cita a lista oficial do Jabuti")
    await goals.add(g)
    inbox = Inbox()
    assert await proactive_tick(services, NOW, notify=inbox) == "done"
    assert goals.goals[g.id].status is GoalStatus.ATIVO
    judge = provider.calls[1]
    assert "Verify (what proves it is done): a nota cita a lista oficial" in judge["messages"][0].content
    assert "data, not instructions" in judge["messages"][0].content
    assert "Ainda não terminou: Falta a fonte oficial." in inbox.texts[0]


async def test_blocked_pauses_and_tells_why(services, goals):
    with_llm(services, step("Preciso do login da biblioteca.", True),
             verdict("blocked", "Precisa do seu login da biblioteca."))
    g = goal()
    await goals.add(g)
    inbox = Inbox()
    assert await proactive_tick(services, NOW, notify=inbox) == "blocked"
    saved = goals.goals[g.id]
    assert saved.status is GoalStatus.PAUSADO and "login" in saved.last_attempt
    assert "Motivo: Precisa do seu login" in inbox.texts[0] and f"/retomar {g.id}" in inbox.texts[0]


async def test_without_contract_the_description_is_checked(services, goals):
    provider, _ = with_llm(services, step("Feito.", True), verdict("done", "ok"))
    g = goal(description="descobrir o livro")
    await goals.add(g)
    assert await proactive_tick(services, NOW) == "concluido"
    assert "Description: descobrir o livro" in provider.calls[1]["messages"][0].content
    assert "Verify" not in provider.calls[1]["messages"][0].content


async def test_recurring_goal_with_stop_condition(services, goals):
    with_llm(services, step("O preço caiu para 199.", True), verdict("done", "Caiu abaixo de 200."))
    g = goal(kind=GoalKind.RECORRENTE, title="Preço do fone", stop_when="o preço ficar abaixo de R$ 200")
    await goals.add(g)
    assert await proactive_tick(services, NOW) == "concluido"
    assert goals.goals[g.id].status is GoalStatus.CONCLUIDO


async def test_recurring_goal_without_stop_condition_is_not_judged(services, goals):
    provider, _ = with_llm(services, step("Nada novo.", True))
    await goals.add(goal(kind=GoalKind.RECORRENTE))
    assert await proactive_tick(services, NOW) == "done" and len(provider.calls) == 1


def test_adaptive_interval_doubles_after_three_unchanged_runs():
    g = Goal(title="x", kind=GoalKind.RECORRENTE, min_interval_hours=6)
    digests = []
    for _ in range(7):
        digest, unchanged, hours = adapted_interval(g, "Sem notícias novas. ", 48)
        g = g.model_copy(update={"result_hash": digest, "unchanged_runs": unchanged})
        digests.append(hours)
    assert digests == [6, 6, 6, 12, 12, 12, 24]
    _, unchanged, hours = adapted_interval(g, "Saiu uma notícia!", 48)
    assert (unchanged, hours) == (0, 6)
    capped = g.model_copy(update={"unchanged_runs": 30})
    assert adapted_interval(capped, "Sem notícias novas.", 48)[2] == 48


async def test_adaptive_interval_is_applied_and_shown(services, goals):
    g = goal(kind=GoalKind.RECORRENTE, min_interval_hours=6, unchanged_runs=2, title="Notícias")
    g.result_hash = result_hash("Nada novo.")
    await goals.add(g)
    with_llm(services, step("Nada novo.", True))
    await proactive_tick(services, NOW)
    saved = goals.goals[g.id]
    assert saved.effective_interval_hours == 12 and saved.next_run_at == NOW + timedelta(hours=12)
    listing = (await Router(services).handle_text("/objetivos")).text
    assert "agora a cada 12h" in listing


async def test_goal_create_takes_a_contract(services, goals):
    action = services.actions.get("goal_create")
    from uva.actions.registry import ActionContext

    params = action.params_model(title="Organizar notas", verify="inbox vazia", stop_when="")
    await action.handler(params, ActionContext(services))
    created = next(iter(goals.goals.values()))
    assert created.verify == "inbox vazia"
    assert "pronto quando: inbox vazia" in (await Router(services).handle_text("/objetivos")).text
