import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fakes import FakeBusy, InMemoryGoalStore, with_llm
from test_routines import InMemoryRoutineStore, Inbox

from uva.brain.vault import Vault
from uva.goals.runner import proactive_tick
from uva.goals.store import Goal
from uva.incidents import signature
from uva.router import Router
from uva.routines import Every, Routine, classify, run_due
from uva.storage.sqlite import Database

NOW = datetime(2026, 10, 13, 15, 0, tzinfo=UTC)


def reply(text: str) -> str:
    return json.dumps({"actions": [], "reply": text})


@pytest.fixture
def rs(services, tmp_path):
    services.routines = InMemoryRoutineStore()
    db = Database(tmp_path / "uva.db")
    db.migrate()
    services.sqlite = db
    return services


async def add(services, **kw) -> Routine:
    routine = Routine(**{"request": "vê se o site caiu", "every": Every.HOURS, "hours": 1, "next_run_at": NOW, **kw})
    await services.routines.add(routine)
    return routine


async def due_again(services, routine_id, at):
    await services.routines.update(routine_id, next_run_at=at)


def test_classify():
    assert classify("[SILENT]") == ("silent", "")
    assert classify("[silencio]") == ("silent", "")
    assert classify("[FAILED]\nsite fora do ar") == ("failed", "site fora do ar")
    assert classify("[FALHOU]") == ("failed", "")
    assert classify("Tudo certo, [FAILED] só no meio") == ("ok", "Tudo certo, [FAILED] só no meio")


def test_signature_ignores_numbers_and_durations():
    assert signature("Timeout after 31s (attempt 2)") == signature("timeout after 58s (attempt 3)")
    assert signature("HTTP 503") == signature("http 502")
    assert signature("connection refused") != signature("dns failure")


async def test_silent_reply_sends_nothing(rs):
    provider, _ = with_llm(rs, reply("[SILENT]"))
    routine = await add(rs)
    inbox = Inbox()
    assert await run_due(rs, inbox, NOW) == [routine.id]
    assert inbox.texts == [] and rs.routines.items[routine.id].last_status == "silent"
    # The LLM is told it is a scheduled run and how to stay silent.
    message = provider.calls[0]["messages"][-1].content
    assert f"routine {routine.id}" in message and "[SILENT]" in message


async def test_same_failure_alerts_once_then_reminds(rs):
    rs.settings.routine_failure_remind_hours = 6
    with_llm(rs, reply("[FAILED]\nsite fora (HTTP 503)"), reply("[FAILED]\nsite fora (HTTP 502)"),
             reply("[FAILED]\nsite fora (HTTP 503)"), reply("Voltou."), reply("[FAILED]\nsite fora (HTTP 500)"))
    routine = await add(rs)
    inbox = Inbox()
    await run_due(rs, inbox, NOW)
    assert len(inbox.texts) == 1 and "❌ falhou: site fora (HTTP 503)" in inbox.texts[0]

    await due_again(rs, routine.id, NOW + timedelta(hours=1))
    await run_due(rs, inbox, NOW + timedelta(hours=1))
    assert len(inbox.texts) == 1  # same incident: no new alert

    await due_again(rs, routine.id, NOW + timedelta(hours=7))
    await run_due(rs, inbox, NOW + timedelta(hours=7))
    assert len(inbox.texts) == 2 and "3 vezes" in inbox.texts[1]  # reminder

    await due_again(rs, routine.id, NOW + timedelta(hours=8))
    await run_due(rs, inbox, NOW + timedelta(hours=8))
    assert "Voltou." in inbox.texts[2]  # a success re-arms

    await due_again(rs, routine.id, NOW + timedelta(hours=9))
    await run_due(rs, inbox, NOW + timedelta(hours=9))
    assert len(inbox.texts) == 4 and "falhou" in inbox.texts[3]

    listing = (await Router(rs).handle_text(f"/rotina {routine.id} incidentes")).text
    assert "aberta · 1x" in listing and "resolvida · 3x" in listing


class FakeShell:
    def __init__(self, result, risks=()):
        self.result, self.reasons, self.codes = result, list(risks), []

    async def risks(self, kind, code):
        return self.reasons

    async def run(self, code, kind="sh", confirmed=None):
        self.codes.append(code)
        return self.result


async def test_script_routine_sends_stdout_without_the_llm(rs):
    provider, _ = with_llm(rs)
    rs.shell = FakeShell({"output": "dólar: 5,10\n", "exit": 0})
    routine = await add(rs, request="script checks/dolar.sh", script="checks/dolar.sh")
    inbox = Inbox()
    await run_due(rs, inbox, NOW)
    assert "dólar: 5,10" in inbox.texts[0] and provider.calls == []
    assert rs.shell.codes == ["cd /uva && bash checks/dolar.sh"]
    assert rs.routines.items[routine.id].last_output == "dólar: 5,10"


async def test_script_routine_empty_output_is_silent_and_errors_alert(rs):
    rs.shell = FakeShell({"output": "", "exit": 0})
    await add(rs, request="script x.py", script="x.py")
    inbox = Inbox()
    await run_due(rs, inbox, NOW)
    assert inbox.texts == []
    rs.shell = FakeShell({"output": "Traceback: boom", "exit": 1})
    await add(rs, id="r2bad", request="script y.py", script="y.py")
    await run_due(rs, inbox, NOW)
    assert "código de saída 1" in inbox.texts[0] and "boom" in inbox.texts[0]
    rs.shell = FakeShell({"output": "", "exit": 0}, risks=["apaga arquivos"])
    await add(rs, id="r3risk", request="script z.sh", script="z.sh")
    await run_due(rs, inbox, NOW)
    assert "parece destrutivo" in inbox.texts[1]


async def test_create_script_and_chained_routines(rs):
    router = Router(rs)
    text = (await router.handle_text("/rotina todo dia 08:00 script checks/site.sh")).text
    assert text.startswith("⏰ Rotina r")
    first = next(iter(rs.routines.items.values()))
    assert first.script == "checks/site.sh"
    bad = (await router.handle_text("/rotina todo dia 08:00 script ../etc/passwd")).text
    assert "inválido" in bad
    await rs.routines.update(first.id, last_output="site ok, 120ms")
    await router.handle_text(f"/rotina todo dia 09:00 resume o relatório depois-de {first.id}")
    second = [r for r in rs.routines.items.values() if r.id != first.id][0]
    assert second.context_from == first.id and second.request == "resume o relatório"
    provider, _ = with_llm(rs, reply("Resumo: site ok."))
    await rs.routines.update(second.id, next_run_at=NOW)
    await rs.routines.update(first.id, next_run_at=NOW + timedelta(days=1))
    await run_due(rs, Inbox(), NOW)
    assert "site ok, 120ms" in provider.calls[0]["messages"][-1].content
    listing = (await router.handle_text("/rotinas")).text
    assert "script: checks/site.sh" in listing and f"usa a última saída de {first.id}" in listing


async def test_goal_run_can_be_silent(services, tmp_path):
    services.goals = InMemoryGoalStore()
    services.busy = FakeBusy()
    services.vault = Vault(tmp_path / "brain", ZoneInfo("America/Sao_Paulo"))
    services.vault.ensure_structure()
    with_llm(services, json.dumps({"actions": [], "reply": "[SILENT]", "done": True}))
    await services.goals.add(Goal(title="Notícias do Avaí", next_run_at=NOW - timedelta(minutes=1)))
    inbox = Inbox()
    assert await proactive_tick(services, NOW, notify=inbox) == "done"
    assert inbox.texts == []
