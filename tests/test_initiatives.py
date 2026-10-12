import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fakes import FakeBusy, FakeSearch, InMemoryGoalStore, InMemoryMemoryStore, InMemoryUsageStore, with_llm

from uva.brain.vault import Vault
from uva.goals.runner import complete_goal_for_feature, in_quiet_hours, proactive_tick
from uva.goals.store import Goal, GoalKind, GoalStatus
from uva.initiatives import InitiativeRecord, initiative_tick
from uva.llm.budget import Budget, Scope, UsageRecord
from uva.memory import MemoryService
from uva.router import Router
from uva.search.base import SearchResult

NOON = datetime(2026, 10, 12, 15, 0, tzinfo=UTC)  # 12:00 in São Paulo
NIGHT = datetime(2026, 10, 12, 4, 0, tzinfo=UTC)  # 01:00 in São Paulo


class InMemoryInitiativeStore:
    def __init__(self) -> None:
        self.records: list[InitiativeRecord] = []
        self.enabled: bool | None = None

    async def add(self, record):
        self.records.append(record)

    async def recent(self, limit):
        return sorted(self.records, key=lambda r: r.at, reverse=True)[:limit]

    async def count_since(self, since):
        return sum(1 for r in self.records if r.at >= since)

    async def get_enabled(self):
        return self.enabled

    async def set_enabled(self, enabled):
        self.enabled = enabled


def decision(**kw) -> str:
    return json.dumps({"kind": "nothing", "topic": "", "message": "", "reason": "teste", **kw})


@pytest.fixture
def init_services(services, tmp_path):
    services.initiatives = InMemoryInitiativeStore()
    services.busy = FakeBusy()
    services.goals = InMemoryGoalStore()
    services.memory = MemoryService(InMemoryMemoryStore(), services.settings)
    services.vault = Vault(tmp_path / "brain", ZoneInfo("America/Sao_Paulo"))
    services.vault.ensure_structure()
    services.settings.proactive_quiet_start_hour = 23
    services.settings.proactive_quiet_end_hour = 8
    return services


# --- quiet hours / goal fixes ----------------------------------------------------------


def test_quiet_hours_cross_midnight(init_services):
    assert in_quiet_hours(init_services, NIGHT)
    assert not in_quiet_hours(init_services, NOON)


async def test_goal_loop_sleeps_in_quiet_hours(init_services):
    with_llm(init_services)
    await init_services.goals.add(Goal(title="x", next_run_at=NIGHT - timedelta(hours=1)))
    assert await proactive_tick(init_services, NIGHT) == "quiet"


async def test_goal_create_defaults_to_one_off_task(init_services):
    plan = {"actions": [{"type": "goal_create", "params": {"title": "Criar um comando de painel"}}], "reply": ""}
    with_llm(init_services, json.dumps(plan))
    await Router(init_services).handle_text("cria um objetivo de fazer um painel")
    goal = next(iter(init_services.goals.goals.values()))
    assert goal.kind is GoalKind.UNICO and goal.min_interval_hours == 6


async def test_merged_feature_completes_its_goal(init_services):
    goal = Goal(id="g1", title="Criar painel")
    await init_services.goals.add(goal)
    completed = await complete_goal_for_feature(init_services, "g1", "painel")
    assert completed.title == "Criar painel"
    stored = init_services.goals.goals["g1"]
    assert stored.status is GoalStatus.CONCLUIDO and stored.last_attempt == "concluído: /painel pronto"
    assert await complete_goal_for_feature(init_services, "g1", "painel") is None  # only once


# --- initiatives -------------------------------------------------------------------------


async def test_research_initiative_sends_summary_and_saves_note(init_services, event_store):
    services = init_services
    services.search = {"web": FakeSearch("web", SearchResult("Jabuti", "https://jabuti.example", "prêmio"))}
    await services.memory.save("leitura", "Leu Marmitas frias e requentadas, de Ricardo Terto")
    provider, _ = with_llm(
        services,
        decision(kind="research", topic="Ricardo Terto Prêmio Jabuti", message="Lembra do Terto? Fui conferir:"),
        "Ricardo Terto foi semifinalista do Jabuti.",
    )
    status, message = await initiative_tick(services, NOON)

    assert status == "sent"
    assert message.startswith("🧠 Lembra do Terto? Fui conferir:")
    assert "semifinalista do Jabuti" in message and "https://jabuti.example" in message
    assert "📝 03-recursos/Pesquisa - Ricardo Terto Prêmio Jabuti.md" in message
    assert "Ricardo Terto" in provider.calls[0]["messages"][0].content  # memory went into the context
    assert services.initiatives.records[0].kind == "research"
    assert event_store.of_type("initiative")[-1].data["status"] == "sent"


async def test_message_initiative(init_services):
    with_llm(init_services, decision(kind="message", message="E aí, voltou pro livro do Terto ou largou de vez?"))
    status, message = await initiative_tick(init_services, NOON)
    assert status == "sent" and message == "💬 E aí, voltou pro livro do Terto ou largou de vez?"


async def test_nothing_sends_nothing(init_services):
    with_llm(init_services, decision(kind="nothing"))
    assert await initiative_tick(init_services, NOON) == ("nothing", None)
    assert init_services.initiatives.records == []


@pytest.mark.parametrize("case", ["off", "quiet", "daily_cap", "gap", "busy", "goal_due"])
async def test_gates(init_services, case):
    services = init_services
    provider, _ = with_llm(services)
    now = NOON
    if case == "off":
        services.initiatives.enabled = False
    elif case == "quiet":
        now = NIGHT
    elif case == "daily_cap":
        for i in range(services.settings.proactive_max_messages_per_day):
            services.initiatives.records.append(InitiativeRecord(kind="message", at=NOON - timedelta(hours=3, minutes=i)))
    elif case == "gap":
        services.initiatives.records.append(InitiativeRecord(kind="message", at=NOON - timedelta(minutes=10)))
    elif case == "busy":
        await services.busy.mark("goal:abc", 60)
    elif case == "goal_due":
        await services.goals.add(Goal(title="vencido", next_run_at=NOON - timedelta(minutes=1)))
    assert await initiative_tick(services, now) == (case, None)
    assert provider.calls == []


async def test_budget_is_respected_even_when_forced(init_services):
    services = init_services
    provider, usage = with_llm(services)
    usage.records.append(UsageRecord(provider="p", model="m", input_tokens=0, output_tokens=0, purpose="t",
                                     cost_usd=services.settings.proactive_daily_budget_usd, scope=Scope.PROACTIVE))
    assert await initiative_tick(services, NOON, force=True) == ("budget", None)
    assert provider.calls == []


async def test_proativo_command(init_services):
    services = init_services
    router = Router(services)
    status = (await router.handle_text("/proativo")).text
    assert "Iniciativas: ligadas" in status and "Silêncio: 23h–8h" in status
    assert "desligadas" in (await router.handle_text("/proativo off")).text
    assert services.initiatives.enabled is False
    assert "ligadas" in (await router.handle_text("/proativo on")).text

    with_llm(services, decision(kind="message", message="Bom dia, parceiro!"))
    assert (await router.handle_text("/proativo agora")).text == "💬 Bom dia, parceiro!"


def test_initiative_tick_is_offset_from_the_goal_tick():
    from uva.tasks.app import app

    schedule = app.conf.beat_schedule
    assert schedule["initiative-tick"]["schedule"] != schedule["proactive-tick"]["schedule"]
