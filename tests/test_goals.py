import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fakes import FakeBusy, FakeSearch, InMemoryGoalStore, with_llm

from uva.brain.vault import Vault
from uva.goals.runner import proactive_tick
from uva.goals.store import Goal, GoalKind, GoalStatus, pick_due
from uva.llm.budget import Scope, UsageRecord
from uva.logging.events import EventType
from uva.router import Router
from uva.search.base import SearchResult

NOW = datetime(2026, 10, 11, 15, 0, tzinfo=UTC)


def step(*actions: dict, reply: str = "", done: bool = True) -> str:
    return json.dumps({"actions": list(actions), "reply": reply, "done": done})


@pytest.fixture
def goals(services, tmp_path) -> InMemoryGoalStore:
    services.goals = InMemoryGoalStore()
    services.busy = FakeBusy()
    services.vault = Vault(tmp_path / "brain", ZoneInfo("America/Sao_Paulo"))
    services.vault.ensure_structure()
    return services.goals


def goal(**kw) -> Goal:
    defaults = {"title": "Energia solar", "next_run_at": NOW - timedelta(minutes=1)}
    return Goal(**{**defaults, **kw})


def test_pick_due_orders_by_priority_then_age():
    a = goal(title="a", priority=2, next_run_at=NOW - timedelta(hours=5))
    b = goal(title="b", priority=5)
    c = goal(title="c", priority=5, next_run_at=NOW - timedelta(hours=2))
    later = goal(title="later", priority=5, next_run_at=NOW + timedelta(hours=1))
    paused = goal(title="paused", priority=5, status=GoalStatus.PAUSADO)
    assert [g.title for g in pick_due([a, b, c, later, paused], NOW)] == ["c", "b", "a"]


async def test_nothing_due_is_quiet(services, goals, event_store):
    with_llm(services)
    await goals.add(goal(next_run_at=NOW + timedelta(hours=1)))
    assert await proactive_tick(services, NOW) == "idle"
    assert event_store.events == []


async def test_busy_bot_skips_the_turn(services, goals, event_store):
    provider, _ = with_llm(services)
    await goals.add(goal())
    await services.busy.mark("chat", 120)
    assert await proactive_tick(services, NOW) == "busy"
    assert provider.calls == []
    assert event_store.of_type(EventType.GOAL_RUN)[0].data["reason"] == "busy"


async def test_budget_exhausted_skips_the_turn(services, goals):
    provider, usage = with_llm(services)
    usage.records.append(
        UsageRecord(
            provider="p", model="m", input_tokens=0, output_tokens=0, scope=Scope.PROACTIVE,
            cost_usd=services.settings.proactive_daily_budget_usd, purpose="goal",
        )
    )
    await goals.add(goal())
    assert await proactive_tick(services) == "budget"
    assert provider.calls == []


async def test_goal_run_searches_saves_note_and_reschedules(services, goals, event_store):
    services.search = {"web": FakeSearch("web", SearchResult("Solar", "https://solar.example", "x"))}
    provider, _ = with_llm(
        services,
        step({"type": "search", "params": {"query": "energia solar"}}, reply="Pesquisei.", done=False),
        "Resumo sobre energia solar.",  # summary of the search, by the heavy model
        step(reply="Nada mais a fazer hoje.", done=True),
    )
    g = goal(min_interval_hours=12)
    await goals.add(g)
    assert await proactive_tick(services, NOW) == "done"

    updated = goals.goals[g.id]
    assert updated.run_count == 1 and updated.last_run_at == NOW
    assert updated.next_run_at == NOW + timedelta(hours=12)
    assert updated.status is GoalStatus.ATIVO
    assert "Pesquisei." in updated.results[-1].summary

    note = services.vault.read("Objetivo - Energia solar")
    assert note.path.startswith("01-projetos/")
    assert "Resumo sobre energia solar." in note.body
    # The research itself also became a reference note.
    assert services.vault.read("Pesquisa - energia solar").path.startswith("03-recursos/")

    # Uses the heavy model, proactive scope, and only safe actions in the prompt.
    assert provider.calls[0]["model"] == "heavy-model"
    assert "goal_create" not in provider.calls[0]["system"]
    assert "run_command" not in provider.calls[0]["system"]
    assert "memory_forget" not in provider.calls[0]["system"]
    assert services.busy.markers == {}
    run_event = event_store.of_type(EventType.GOAL_RUN)[-1]
    assert run_event.data["status"] == "done" and run_event.data["steps"] == 2


async def test_unsafe_action_aborts_the_run(services, goals, event_store):
    provider, _ = with_llm(services, step({"type": "run_command", "params": {"command": "ping"}}))
    g = goal()
    await goals.add(g)
    assert await proactive_tick(services, NOW) == "failed"
    assert event_store.of_type(EventType.ACTION) == []
    assert goals.goals[g.id].results[-1].ok is False


async def test_single_goal_is_concluded(services, goals):
    verdict = json.dumps({"verdict": "done", "reason": "Feito e conferido."})
    with_llm(services, step(reply="Feito.", done=True), verdict)
    g = goal(kind=GoalKind.UNICO)
    await goals.add(g)
    assert await proactive_tick(services, NOW) == "concluido"
    assert goals.goals[g.id].status is GoalStatus.CONCLUIDO


async def test_step_limit(services, goals):
    services.settings.proactive_max_steps = 2
    provider, _ = with_llm(services, step(reply="1", done=False), step(reply="2", done=False))
    # Without actions the loop stops after the first step even if not done.
    await goals.add(goal())
    await proactive_tick(services, NOW)
    assert len(provider.calls) == 1


async def test_token_limit(services, goals):
    services.settings.proactive_max_tokens_per_run = 100  # each fake call uses 150
    services.search = {"web": FakeSearch("web")}
    provider, _ = with_llm(
        services,
        step({"type": "note_search", "params": {"term": "x"}}, done=False),
        step(reply="nunca chamado"),
    )
    g = goal()
    await goals.add(g)
    await proactive_tick(services, NOW)
    assert len(provider.calls) == 1
    assert "tokens" in goals.goals[g.id].results[-1].summary


async def test_goal_create_and_commands(services, goals):
    create = json.dumps(
        {
            "actions": [
                {
                    "type": "goal_create",
                    "params": {"title": "Notícias de energia solar", "priority": 4, "min_interval_hours": 168},
                }
            ],
            "reply": "Combinado!",
        }
    )
    with_llm(services, create)
    router = Router(services)
    reply = await router.handle_text("acompanha notícias de energia solar toda semana")
    goal_id = next(iter(goals.goals))
    assert f"🎯 Objetivo criado ({goal_id})" in reply.text

    listing = (await router.handle_text("/objetivos")).text
    assert goal_id in listing and "prioridade 4" in listing and "a cada 168h" in listing
    assert "pausado" in (await router.handle_text(f"/pausar {goal_id}")).text
    assert goals.goals[goal_id].status is GoalStatus.PAUSADO
    assert "ativo" in (await router.handle_text(f"/retomar {goal_id}")).text
    assert "removido" in (await router.handle_text(f"/remover {goal_id}")).text
    assert goals.goals == {}


class Inbox:
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def __call__(self, reply) -> None:
        self.texts.append(reply.text)


async def test_finished_goal_run_notifies_owner(services, goals):
    with_llm(services, step(reply="Achei três novidades.", done=True))
    await goals.add(goal())
    inbox = Inbox()
    assert await proactive_tick(services, NOW, notify=inbox) == "done"
    assert inbox.texts[0].startswith('🎯 Objetivo "Energia solar" rodou')
    assert "Achei três novidades." in inbox.texts[0]
    assert "📝 01-projetos/objetivo_energia_solar.md" in inbox.texts[0]


async def test_skipped_turn_is_not_announced(services, goals):
    with_llm(services)
    await goals.add(goal())
    await services.busy.mark("chat", 60)
    inbox = Inbox()
    assert await proactive_tick(services, NOW, notify=inbox) == "busy"
    assert inbox.texts == []


# --- last attempt, spacing, schedule, features ------------------------------------------


def test_beat_polls_on_the_wall_clock():
    from celery.schedules import crontab

    from uva.tasks.app import app

    schedule = app.conf.beat_schedule["proactive-tick"]["schedule"]
    assert isinstance(schedule, crontab)  # not a timedelta reset by every restart


async def test_skips_are_recorded_as_last_attempt(services, goals):
    with_llm(services)
    g = goal()
    await goals.add(g)
    await services.busy.mark("chat", 60)
    await proactive_tick(services, NOW)
    assert goals.goals[g.id].last_attempt == "pulado: ocupado (chat)"
    assert goals.goals[g.id].last_attempt_at == NOW
    text = (await Router(services).handle_text("/objetivos")).text
    assert "última tentativa: " in text and "pulado: ocupado" in text and "execuções: 0" in text


async def test_runs_are_spaced_by_the_interval(services, goals):
    with_llm(services, step(reply="Feito.", done=True))
    ran = goal(title="já rodou", last_run_at=NOW - timedelta(minutes=10), next_run_at=NOW + timedelta(hours=5))
    waiting = goal(title="esperando")
    await goals.add(ran)
    await goals.add(waiting)
    assert await proactive_tick(services, NOW) == "interval"
    assert goals.goals[waiting.id].last_attempt == "pulado: intervalo entre execuções"
    assert await proactive_tick(services, NOW + timedelta(minutes=61)) == "done"


async def test_never_attempted_goal_says_so(services, goals):
    await goals.add(goal())
    text = (await Router(services).handle_text("/objetivos")).text
    assert "última tentativa: nunca · execuções: 0" in text and "(atrasada" in text


def feature_step():
    return json.dumps(
        {"actions": [], "reply": "Isso precisa de um comando.", "done": True,
         "needs_feature": "/painel mostra memória e objetivos"}
    )


@pytest.fixture
def features(services):
    from uva.selfmod.features import FeatureService
    from uva.selfmod.git import GitRepo
    from test_selfcode import InMemoryFeatureStore

    enqueued: list[str] = []

    async def enqueue(feature_id):
        enqueued.append(feature_id)

    services.features = FeatureService(InMemoryFeatureStore(), enqueue)
    services.repo = GitRepo(services.settings.repo_dir, "t", "t@t")  # only checked for presence
    return enqueued


async def test_goal_requests_feature_once_and_notifies(services, goals, features):
    with_llm(services, feature_step(), feature_step())
    g = goal(min_interval_hours=1)
    await goals.add(g)
    inbox = Inbox()

    assert await proactive_tick(services, NOW, notify=inbox) == "needs_feature"
    assert len(features) == 1
    record = services.features.store.records[features[0]]
    assert record.goal_id == g.id and record.description == "/painel mostra memória e objetivos"
    assert "pedi:" in inbox.texts[0] and f"feature {record.id}" in inbox.texts[0]
    assert goals.goals[g.id].status is GoalStatus.ATIVO
    assert goals.goals[g.id].last_attempt == f"pediu a feature {record.id}"

    # Next run: the same feature is already on its way, so nothing new is requested.
    later = NOW + timedelta(hours=2)
    await proactive_tick(services, later, notify=inbox)
    assert len(features) == 1
    assert "já existe a feature" in inbox.texts[1]
    assert goals.goals[g.id].status is GoalStatus.ATIVO


async def test_goal_feature_disabled_pauses_with_suggestion(services, goals, features):
    services.settings.proactive_allow_selfcode = False
    with_llm(services, feature_step())
    g = goal()
    await goals.add(g)
    inbox = Inbox()
    await proactive_tick(services, NOW, notify=inbox)
    assert features == []
    assert "Se quiser, manda: /feature /painel mostra memória e objetivos" in inbox.texts[0]
    assert goals.goals[g.id].status is GoalStatus.PAUSADO
    assert goals.goals[g.id].needs_feature == "/painel mostra memória e objetivos"


async def test_goal_commands_in_english(services, goals):
    from uva import i18n

    i18n.set_language("en")
    router = Router(services)
    assert (await router.handle_text("/goals")).text.startswith("No goals yet.")
    await goals.add(goal(title="Solar news", priority=4, min_interval_hours=168))
    goal_id = next(iter(goals.goals))
    listing = (await router.handle_text("/goals")).text
    assert listing.startswith("Goals:")
    assert "▶️ active · priority 4" in listing and "every 168h" in listing
    assert "last attempt: never" in listing and "(overdue; runs as soon as possible)" in listing
    assert (await router.handle_text(f"/pause {goal_id}")).text.endswith("⏸ paused.")
    assert "(overdue" not in (await router.handle_text("/goals")).text  # paused goals have no next run
    assert (await router.handle_text("/pause")).text == "Usage: /pause <id> (ids in /goals)"
    assert "removed" in (await router.handle_text(f"/remove {goal_id}")).text


def test_goal_reports_in_english():
    from uva import i18n
    from uva.goals.runner import GoalRun, describe_run

    i18n.set_language("en")
    run = GoalRun(Goal(id="g1", title="Read more"), summaries=["Found 3 books."])
    assert describe_run(run, "done", "01-projetos/x.md") == (
        '🎯 Goal "Read more" ran\nFound 3 books.\n📝 01-projetos/x.md'
    )
    stuck = GoalRun(Goal(id="g1", title="Read more"), needs_feature="list my books")
    text = describe_run(stuck, "needs_feature", None)
    assert "needs a new command" in text and "If you want it, send: /feature list my books" in text
    assert "/resume g1" in text
