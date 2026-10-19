import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fakes import FakeBusy, InMemoryGoalStore, InMemoryUsageStore, make_settings
from test_routines import InMemoryRoutineStore, Inbox

from uva.brain.vault import Vault
from uva.goals.runner import proactive_tick
from uva.goals.store import Goal
from uva.llm.base import LLMError, LLMMessage, LLMResponse, status_error
from uva.llm.budget import Budget, Scope
from uva.llm.client import Fallback, LLMClient, build_llm_client
from uva.llm.pricing import PriceTable
from uva.logging.events import EventType
from uva.routines import Every, Routine, run_due
from uva.storage.sqlite import Database

NOW = datetime(2026, 10, 13, 15, 0, tzinfo=UTC)


class Flaky:
    """Fails with the given errors in order, then answers."""

    def __init__(self, name="primary", errors=(), reply='{"actions": [], "reply": "ok"}'):
        self.name = name
        self.errors = list(errors)
        self.reply = reply
        self.calls = []

    async def complete(self, messages, *, model, system=None, json_schema=None, max_tokens=4000):
        self.calls.append(model)
        if self.errors:
            raise self.errors.pop(0)
        return LLMResponse(text=self.reply, model=model, input_tokens=1_000_000, output_tokens=0)


def transient(retry_after=None):
    return LLMError("429 rate limit", status=429, retry_after=retry_after, transient=True)


def client(provider, usage=None, fallback=None, retries=3):
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)

    llm = LLMClient(
        provider, router_model="router-model", heavy_model="heavy-model",
        prices=PriceTable({"router-model": (1.0, 2.0), "backup-model": (3.0, 4.0)}, (5.0, 25.0)),
        usage=usage, max_retries=retries, fallback=fallback, sleep=sleep,
    )
    return llm, sleeps


async def call(llm):
    return await llm.complete([LLMMessage("user", "oi")], purpose="t", scope=Scope.INTERACTIVE)


async def test_transient_errors_are_retried_with_retry_after(event_store):
    provider = Flaky(errors=[transient(retry_after=2), transient(retry_after=2)])
    llm, sleeps = client(provider)
    response = await call(llm)
    assert response.text and len(provider.calls) == 3 and sleeps == [2, 2]
    retries = [e for e in event_store.of_type(EventType.ERROR) if e.data["where"] == "llm_retry"]
    assert [e.data["attempt"] for e in retries] == [1, 2]


async def test_exponential_backoff_then_give_up(event_store):
    provider = Flaky(errors=[transient()] * 5)
    llm, sleeps = client(provider)
    with pytest.raises(LLMError):
        await call(llm)
    assert len(provider.calls) == 4 and sleeps == [1, 2, 4]


async def test_other_errors_are_not_retried(event_store):
    provider = Flaky(errors=[LLMError("400 bad request", status=400)])
    llm, sleeps = client(provider)
    with pytest.raises(LLMError):
        await call(llm)
    assert len(provider.calls) == 1 and sleeps == []


async def test_fallback_after_retries(event_store):
    primary = Flaky(errors=[transient()] * 4)
    backup = Flaky(name="backup")
    usage = InMemoryUsageStore()
    llm, _ = client(primary, usage, fallback=Fallback(backup, "backup-model"))
    response = await call(llm)
    assert response.model == "backup-model" and backup.calls == ["backup-model"]
    record = usage.records[0]
    assert (record.provider, record.model, record.cost_usd) == ("backup", "backup-model", 3.0)
    event = event_store.of_type(EventType.LLM_CALL)[0]
    assert event.data["fallback"] is True and event.data["provider"] == "backup"


def test_status_error_keeps_status_and_retry_after():
    exc = SimpleNamespace(status_code=503, response=SimpleNamespace(headers={"retry-after": "7"}))
    error = status_error("openai", exc)
    assert (error.status, error.retry_after, error.transient) == (503, 7.0, True)
    bad = status_error("openai", SimpleNamespace(status_code=401, response=None))
    assert not bad.transient
    network = status_error("openai", RuntimeError("connection reset"))
    assert network.transient and network.status is None


def test_fallback_settings():
    settings = make_settings(
        llm_provider="openai_compatible", llm_api_key="k", llm_model_router="a", llm_model_heavy="b",
        llm_fallback_provider="openai_compatible", llm_fallback_model="cheap-model",
    )
    llm = build_llm_client(settings, None)
    assert llm.fallback is not None and llm.fallback.model == "cheap-model"
    assert build_llm_client(make_settings(llm_api_key="k", llm_model_router="a", llm_model_heavy="b"), None).fallback is None


def down():
    return LLMError("connection refused", transient=True)


@pytest.fixture
def rs(services, tmp_path):
    services.routines = InMemoryRoutineStore()
    db = Database(tmp_path / "uva.db")
    db.migrate()
    services.sqlite = db
    return services


def attach(services, provider):
    usage = InMemoryUsageStore()
    services.llm, _ = client(provider, usage, retries=0)
    services.budget = Budget(usage, services.settings, services.tz)


async def test_routine_waits_for_the_llm_and_runs_again(rs):
    provider = Flaky(errors=[down(), down()], reply=json.dumps({"actions": [], "reply": "Dólar a 5,10."}))
    attach(rs, provider)
    routine = Routine(id="r1", request="vê o dólar", every=Every.DAY, at="08:00", next_run_at=NOW)
    await rs.routines.add(routine)
    inbox = Inbox()
    await run_due(rs, inbox, NOW)
    saved = rs.routines.items["r1"]
    assert saved.waiting_llm and saved.next_run_at == NOW + timedelta(minutes=10)
    assert len(inbox.texts) == 1 and "não respondeu" in inbox.texts[0]

    await run_due(rs, inbox, NOW + timedelta(minutes=10))  # still down: no new alert
    assert len(inbox.texts) == 1 and rs.routines.items["r1"].waiting_llm

    await run_due(rs, inbox, NOW + timedelta(minutes=20))  # back
    assert "Dólar a 5,10." in inbox.texts[1] and not rs.routines.items["r1"].waiting_llm
    assert rs.routines.items["r1"].next_run_at > NOW + timedelta(hours=10)  # next 08:00


async def test_goal_is_requeued_when_the_llm_is_down(services, tmp_path):
    services.goals = InMemoryGoalStore()
    services.busy = FakeBusy()
    services.vault = Vault(tmp_path / "brain", ZoneInfo("America/Sao_Paulo"))
    services.vault.ensure_structure()
    services.settings.proactive_interval_minutes = 0
    attach(services, Flaky(errors=[down(), down()]))
    goal = Goal(title="Notícias", min_interval_hours=24, next_run_at=NOW - timedelta(minutes=1))
    await services.goals.add(goal)
    inbox = Inbox()
    assert await proactive_tick(services, NOW, notify=inbox) == "failed"
    saved = services.goals.goals[goal.id]
    assert saved.waiting_llm and saved.next_run_at == NOW + timedelta(minutes=10)
    assert len(inbox.texts) == 1
    await proactive_tick(services, NOW + timedelta(minutes=10), notify=inbox)
    assert len(inbox.texts) == 1  # the same outage is not reported twice
