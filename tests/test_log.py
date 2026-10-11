from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from uva.commands.log import LogArgsError, parse_log_args
from uva.logging.events import EventType
from uva.router import Router

TZ = ZoneInfo("America/Sao_Paulo")
NOW = datetime(2026, 10, 11, 15, 0, tzinfo=UTC)  # 12:00 in São Paulo


def test_default_period_is_one_hour():
    req = parse_log_args([], TZ, NOW)
    assert req.query.since == NOW - timedelta(hours=1)
    assert req.query.types is None


@pytest.mark.parametrize(
    ("arg", "delta"),
    [("30m", timedelta(minutes=30)), ("6h", timedelta(hours=6)), ("2d", timedelta(days=2))],
)
def test_durations(arg, delta):
    assert parse_log_args([arg], TZ, NOW).query.since == NOW - delta


def test_today_starts_at_local_midnight():
    since = parse_log_args(["hoje"], TZ, NOW).query.since
    assert since == datetime(2026, 10, 11, 3, 0, tzinfo=UTC)


def test_type_filter():
    req = parse_log_args(["1d", "llm"], TZ, NOW)
    assert req.query.types == {EventType.LLM_CALL}
    assert req.query.since == NOW - timedelta(days=1)


def test_trace_detail():
    req = parse_log_args(["trace", "abcd1234"], TZ, NOW)
    assert req.detail and req.query.trace_id == "abcd1234"


def test_invalid_argument():
    with pytest.raises(LogArgsError):
        parse_log_args(["ontem?"], TZ, NOW)


async def test_every_message_is_traced(services, event_store):
    router = Router(services)
    await router.handle_text("/ping")

    traces = {e.trace_id for e in event_store.events}
    assert len(traces) == 1
    types = [e.type for e in event_store.events]
    assert types[0] == EventType.MESSAGE_RECEIVED
    assert EventType.ROUTE in types and EventType.COMMAND in types


async def test_log_shows_path_of_each_message(services):
    router = Router(services)
    await router.handle_text("/ping")
    await router.handle_text("bom dia")

    text = (await router.handle_text("/log 1h")).text
    assert '"/ping" → comando /ping' in text
    assert '"bom dia" → não entendido' in text


async def test_log_trace_detail(services, event_store):
    router = Router(services)
    await router.handle_text("/ping")
    trace_id = event_store.events[0].trace_id

    text = (await router.handle_text(f"/log trace {trace_id}")).text
    assert f"Trace {trace_id}" in text
    assert '"name": "ping"' in text


async def test_long_log_is_sent_as_file(services):
    router = Router(services)
    for _ in range(80):
        await router.handle_text("/ping")
    reply = await router.handle_text("/log 1h")
    assert reply.document is not None
    assert reply.filename == "log.md"


async def test_errors_are_recorded(services, event_store):
    async def boom(ctx):
        raise RuntimeError("kaput")

    services.registry.command("boom", "explode")(boom)
    reply = await Router(services).handle_text("/boom")
    assert "erro" in reply.text.lower()
    errors = event_store.of_type(EventType.ERROR)
    assert errors and "kaput" in errors[0].data["error"]


async def test_status_lists_health_checks(services):
    async def up():
        return True

    async def down():
        return False

    services.health_checks = {"MongoDB": up, "Redis": down}
    text = (await Router(services).handle_text("/status")).text
    assert "🟢 MongoDB" in text and "🔴 Redis" in text
