import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fakes import with_llm

from uva.router import Router
from uva.routines import Every, Routine, next_run, parse, run_due

SP = ZoneInfo("America/Sao_Paulo")
# Tuesday 13/10/2026 12:00 in São Paulo.
NOON = datetime(2026, 10, 13, 15, 0, tzinfo=UTC)


class InMemoryRoutineStore:
    def __init__(self):
        self.items: dict[str, Routine] = {}

    async def add(self, routine):
        self.items[routine.id] = routine

    async def get(self, routine_id):
        return self.items.get(routine_id)

    async def update(self, routine_id, **fields):
        self.items[routine_id] = self.items[routine_id].model_copy(update=fields)

    async def delete(self, routine_id):
        return self.items.pop(routine_id, None) is not None

    async def all(self):
        return list(self.items.values())

    async def due(self, now):
        return [r for r in self.items.values() if r.enabled and r.next_run_at <= now]


class Inbox:
    def __init__(self):
        self.texts = []

    async def __call__(self, reply):
        self.texts.append(reply.text)


@pytest.fixture
def store(services):
    services.routines = InMemoryRoutineStore()
    return services.routines


def test_parse():
    r = parse("todo dia 08:00 pesquisa se a skin Arcana do Jhin saiu na loja do LoL")
    assert (r.every, r.at, r.request) == (Every.DAY, "08:00", "pesquisa se a skin Arcana do Jhin saiu na loja do LoL")
    r = parse("segunda e quinta 9h me lembra de regar as plantas")
    assert (r.every, r.weekdays, r.at, r.request) == (Every.WEEKDAYS, [0, 3], "09:00", "me lembra de regar as plantas")
    r = parse("a cada 6 horas confere o preço do dólar")
    assert (r.every, r.hours, r.request) == (Every.HOURS, 6, "confere o preço do dólar")
    assert parse("todo dia 7h30 manda bom dia").at == "07:30"
    assert parse("todo dia às 8 manda bom dia").request == "manda bom dia"
    assert parse("qualquer coisa sem horário") is None


def test_next_run_daily_and_weekly():
    daily = Routine(request="x", every=Every.DAY, at="08:00")
    assert next_run(daily, NOON, SP) == datetime(2026, 10, 14, 11, 0, tzinfo=UTC)  # tomorrow 08:00 local
    weekly = Routine(request="x", every=Every.WEEKDAYS, weekdays=[0, 3], at="09:00")
    assert next_run(weekly, NOON, SP) == datetime(2026, 10, 15, 12, 0, tzinfo=UTC)  # Thursday 09:00 local


async def test_command_creates_and_lists(services, store):
    reply = (await Router(services).handle_text("/rotina todo dia 08:00 pesquisa se a skin X saiu na loja do LoL")).text
    assert reply.startswith("⏰ Rotina r") and "todo dia às 08:00" in reply
    listing = (await Router(services).handle_text("/rotinas")).text
    assert "pesquisa se a skin X saiu" in listing and "próxima:" in listing


async def test_missed_slots_run_once_at_startup(services, store):
    """PC off from Friday night to Tuesday noon: four 08:00 slots missed, one run."""
    routine = Routine(request="/ping", every=Every.DAY, at="08:00",
                      next_run_at=datetime(2026, 10, 10, 11, 0, tzinfo=UTC))  # Saturday 08:00 local
    await store.add(routine)
    inbox = Inbox()
    assert await run_due(services, inbox, NOON) == [routine.id]
    assert await run_due(services, inbox, NOON + timedelta(minutes=1)) == []  # not again
    assert len(inbox.texts) == 1
    assert "atrasada: era para 10/10 08:00" in inbox.texts[0] and "pong" in inbox.texts[0].lower()
    saved = store.items[routine.id]
    assert saved.runs == 1 and saved.next_run_at == datetime(2026, 10, 14, 11, 0, tzinfo=UTC)


async def test_routine_goes_through_the_llm_like_a_message(services, store):
    plan = {"actions": [], "reply": "Ainda não saiu a skin."}
    with_llm(services, json.dumps(plan))
    await store.add(Routine(request="vê se a skin X saiu", every=Every.HOURS, hours=6, next_run_at=NOON))
    inbox = Inbox()
    await run_due(services, inbox, NOON)
    assert "Ainda não saiu a skin." in inbox.texts[0] and "atrasada" not in inbox.texts[0]


async def test_pause_resume_remove(services, store):
    router = Router(services)
    await router.handle_text("/rotina a cada 6 horas confere o dólar")
    rid = next(iter(store.items))
    assert "pausada" in (await router.handle_text(f"/rotina {rid} pausar")).text
    assert store.items[rid].enabled is False
    assert "retomada" in (await router.handle_text(f"/rotina {rid} retomar")).text
    assert "removida" in (await router.handle_text(f"/rotina {rid} remover")).text
    assert store.items == {}


async def test_llm_creates_routines(services, store):
    plan = {"actions": [{"type": "routine_create", "params": {
        "schedule_and_request": "todo dia 08:00 pesquisa se a skin X saiu na loja do LoL"}}], "reply": ""}
    with_llm(services, json.dumps(plan))
    reply = (await Router(services).handle_text("todo dia às 8 da manhã vê se a skin X saiu na loja do LoL")).text
    assert "⏰ Rotina" in reply and len(store.items) == 1
