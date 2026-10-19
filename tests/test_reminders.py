import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fakes import with_llm
from test_routines import InMemoryRoutineStore, Inbox

from uva import i18n, reminders
from uva.reminders import WhenError, parse_when, purge
from uva.router import Router
from uva.routines import Every, Routine, run_due

SP = ZoneInfo("America/Sao_Paulo")
# Tuesday 13/10/2026 12:00 in São Paulo.
NOON = datetime(2026, 10, 13, 15, 0, tzinfo=UTC)


def local(text: str) -> tuple[str, str] | None:
    found = parse_when(text, NOON, SP)
    return (found.at.astimezone(SP).strftime("%a %d/%m/%Y %H:%M"), found.rest) if found else None


@pytest.mark.parametrize(
    ("text", "when", "rest"),
    [
        ("daqui 20 min ligar pro dentista", "Tue 13/10/2026 12:20", "ligar pro dentista"),
        ("daqui a 2 horas tirar o bolo", "Tue 13/10/2026 14:00", "tirar o bolo"),
        ("em 3 dias pagar a conta", "Fri 16/10/2026 12:00", "pagar a conta"),
        ("daqui meia hora tirar a roupa", "Tue 13/10/2026 12:30", "tirar a roupa"),
        ("amanhã às 9 de ver a previsão", "Wed 14/10/2026 09:00", "ver a previsão"),
        ("amanha 9h ligar", "Wed 14/10/2026 09:00", "ligar"),
        ("amanhã de manhã correr", "Wed 14/10/2026 09:00", "correr"),
        ("amanhã comprar 2 pães", "Wed 14/10/2026 09:00", "comprar 2 pães"),
        ("hoje 18h academia", "Tue 13/10/2026 18:00", "academia"),
        ("sexta 14h reunião", "Fri 16/10/2026 14:00", "reunião"),
        ("segunda-feira às 8:30 dentista", "Mon 19/10/2026 08:30", "dentista"),
        ("terça 13h almoço", "Tue 13/10/2026 13:00", "almoço"),
        # A weekday whose time already passed this week is next week's.
        ("terça 10h almoço", "Tue 20/10/2026 10:00", "almoço"),
        ("dia 25 às 10h aluguel", "Sun 25/10/2026 10:00", "aluguel"),
        # A day of the month already past is next month's.
        ("dia 10 às 10h aluguel", "Tue 10/11/2026 10:00", "aluguel"),
        ("25/12 08:00 natal", "Fri 25/12/2026 08:00", "natal"),
        ("25/12/2026 às 8h natal", "Fri 25/12/2026 08:00", "natal"),
        ("10/10 08:00 aniversário", "Sun 10/10/2027 08:00", "aniversário"),
        ("às 11h almoço", "Wed 14/10/2026 11:00", "almoço"),
        ("ligar pro dentista amanhã às 9h", "Wed 14/10/2026 09:00", "ligar pro dentista"),
        ("in 20 minutes call mom", "Tue 13/10/2026 12:20", "call mom"),
        ("tomorrow 9am gym", "Wed 14/10/2026 09:00", "gym"),
        ("tomorrow at 7:45pm dinner", "Wed 14/10/2026 19:45", "dinner"),
        ("friday 2pm meeting", "Fri 16/10/2026 14:00", "meeting"),
        ("on the 25th at 10 rent", "Sun 25/10/2026 10:00", "rent"),
        ("at 3pm tea", "Tue 13/10/2026 15:00", "tea"),
        ("call mom in 2 hours", "Tue 13/10/2026 14:00", "call mom"),
        ("2026-10-20T09:30", "Tue 20/10/2026 09:30", ""),
    ],
)
def test_parse_when(text, when, rest):
    assert local(text) == (when, rest)


def test_month_rollover_skips_months_without_that_day():
    november = datetime(2026, 11, 2, 15, 0, tzinfo=UTC)
    found = parse_when("dia 31 pagar", november, SP)
    assert found.at.astimezone(SP).strftime("%d/%m/%Y") == "31/12/2026"


def test_no_time_and_bad_times():
    assert parse_when("ligar pro dentista", NOON, SP) is None
    with pytest.raises(WhenError):
        parse_when("amanhã às 25h ligar", NOON, SP)
    with pytest.raises(WhenError):
        parse_when("31/02 08:00 x", NOON, SP)
    assert parse_when("30 min", NOON, SP) is None
    assert parse_when("30 min", NOON, SP, snooze=True).at == NOON + timedelta(minutes=30)


@pytest.fixture
def rs(services):
    services.routines = InMemoryRoutineStore()
    return services


async def test_command_creates_and_lists(rs):
    router = Router(rs)
    text = (await router.handle_text("/lembrete daqui 20 min tirar o bolo do forno")).text
    assert text.startswith("⏰ Lembrete r") and "tirar o bolo do forno" in text
    routine = next(iter(rs.routines.items.values()))
    assert routine.every is Every.ONCE and routine.reminder == "tirar o bolo do forno"
    listing = (await router.handle_text("/lembretes")).text
    assert routine.id in listing and "tirar o bolo" in listing
    assert "Nenhuma rotina" in (await router.handle_text("/rotinas")).text  # reminders stay out


async def test_past_time_and_missing_text_are_refused(rs):
    router = Router(rs)
    assert "já passou" in (await router.handle_text("/lembrete 2020-01-01T08:00")).text
    assert "Uso:" in (await router.handle_text("/lembrete daqui 20 min")).text
    assert "Uso:" in (await router.handle_text("/lembrete comprar pão")).text
    assert rs.routines.items == {}


async def test_plain_reminder_is_sent_without_the_llm(rs):
    provider, _ = with_llm(rs)
    routine = Routine(request="ligar pro dentista", reminder="ligar pro dentista", every=Every.ONCE,
                      once_at=NOON, next_run_at=NOON)
    await rs.routines.add(routine)
    inbox = Inbox()
    replies = []

    async def notify(reply):
        replies.append(reply)
        await inbox(reply)

    assert await run_due(rs, notify, NOON) == [routine.id]
    assert inbox.texts == ["⏰ ligar pro dentista"] and provider.calls == []
    assert [b.data for b in replies[0].buttons] == [f"rmdok:{routine.id}", f"rmd1h:{routine.id}", f"rmdtm:{routine.id}"]
    stored = rs.routines.items[routine.id]
    assert stored.enabled is False and stored.runs == 1
    assert await run_due(rs, notify, NOON + timedelta(hours=1)) == []  # once only


async def test_late_reminder_runs_once_marked_late(rs):
    with_llm(rs)
    routine = Routine(request="x", reminder="regar as plantas", every=Every.ONCE,
                      once_at=NOON - timedelta(hours=10), next_run_at=NOON - timedelta(hours=10))
    await rs.routines.add(routine)
    inbox = Inbox()
    await run_due(rs, inbox, NOON)
    assert len(inbox.texts) == 1 and "regar as plantas" in inbox.texts[0] and "atrasada" in inbox.texts[0]


async def test_reminder_with_a_request_goes_through_the_router(rs):
    provider, _ = with_llm(rs, json.dumps({"actions": [], "reply": "Sol, 25°C."}))
    routine = Routine(request="vê a previsão do tempo", every=Every.ONCE, once_at=NOON, next_run_at=NOON)
    await rs.routines.add(routine)
    inbox = Inbox()
    await run_due(rs, inbox, NOON)
    assert len(provider.calls) == 1 and "Sol, 25°C." in inbox.texts[0]
    assert inbox.texts[0].startswith("⏰ Lembrete: vê a previsão")
    assert rs.routines.items[routine.id].enabled is False


async def test_buttons_snooze_and_done(rs):
    routine = Routine(request="ligar", reminder="ligar", every=Every.ONCE, once_at=NOON, next_run_at=NOON,
                      enabled=False, last_run_at=NOON)
    await rs.routines.add(routine)
    text = await reminders.handle_button(rs, "rmd1h", routine.id, now=NOON)
    stored = rs.routines.items[routine.id]
    assert "adiado" in text and stored.enabled and stored.next_run_at == NOON + timedelta(hours=1)
    await reminders.handle_button(rs, "rmdtm", routine.id, now=NOON + timedelta(hours=1))
    stored = rs.routines.items[routine.id]
    # Tomorrow at the time it was set for (13:00 local after the +1h).
    assert stored.next_run_at.astimezone(SP).strftime("%d/%m %H:%M") == "14/10 13:00"
    assert "Feito" in await reminders.handle_button(rs, "rmdok", routine.id, now=NOON)
    reply = await Router(rs).handle_callback(f"rmd1h:{routine.id}")
    assert "adiado" in reply.text


async def test_snooze_and_cancel_commands(rs):
    router = Router(rs)
    await router.handle_text("/lembrete amanhã às 9 pagar a conta")
    routine = next(iter(rs.routines.items.values()))
    reply = (await router.handle_text(f"/lembrete {routine.id} adiar 2h")).text
    assert "adiado" in reply
    assert "Uso:" in (await router.handle_text(f"/lembrete {routine.id} adiar")).text
    reply = (await router.handle_text(f"/lembrete {routine.id} cancelar")).text
    assert "cancelado" in reply and rs.routines.items == {}
    assert "Não achei" in (await router.handle_text(f"/lembrete {routine.id} cancelar")).text


async def test_maintenance_purges_week_old_reminders(rs):
    old = Routine(request="a", reminder="a", every=Every.ONCE, once_at=NOON, enabled=False,
                  last_run_at=NOON - timedelta(days=8))
    recent = Routine(request="b", reminder="b", every=Every.ONCE, once_at=NOON, enabled=False,
                     last_run_at=NOON - timedelta(days=2))
    pending = Routine(request="c", reminder="c", every=Every.ONCE, once_at=NOON + timedelta(days=30))
    daily = Routine(request="d", every=Every.DAY, enabled=False, last_run_at=NOON - timedelta(days=30))
    for routine in (old, recent, pending, daily):
        await rs.routines.add(routine)
    assert await purge(rs.routines, NOON) == 1
    assert set(rs.routines.items) == {recent.id, pending.id, daily.id}


async def test_llm_creates_reminders(rs):
    plan = {"actions": [{"type": "reminder_create", "params": {"when": "amanhã às 9", "text": "ligar pro João"}}],
            "reply": ""}
    provider, _ = with_llm(rs, json.dumps(plan))
    text = (await Router(rs).handle_text("me lembra amanhã às 9 de ligar pro João")).text
    assert "Lembrete r" in text
    routine = next(iter(rs.routines.items.values()))
    assert routine.reminder == "ligar pro João" and routine.once_at > datetime.now(UTC)
    assert "reminder_create" in provider.calls[0]["system"] and "Never\n  promise a reminder" in provider.calls[0]["system"]


async def test_english_replies(rs):
    i18n.set_language("en")
    router = Router(rs)
    text = (await router.handle_text("/reminder tomorrow 9am call mom")).text
    assert text.startswith("⏰ Reminder r") and "tomorrow 09:00" in text
    assert "Reminders (1 pending)" in (await router.handle_text("/reminders")).text
