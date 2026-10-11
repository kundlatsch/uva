import pytest

from uva.commands import build_registry
from uva.commands.log import extract_log_args
from uva.logging.events import EventType
from uva.matching import best_match
from uva.llm.agent import LLM_DISABLED
from uva.router import CONFIRM_DENIED, Router
from uva.text import extract_duration, normalize


def test_normalize():
    assert normalize("  Olá, Você está AÍ?! ") == "ola voce esta ai"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("log 2 horas", "2h"),
        ("ultimos 30 minutos", "30m"),
        ("nas ultimas 3 hrs", "3h"),
        ("ultimo dia", "1d"),
        ("meia hora", "30m"),
        ("uma hora", "1h"),
        ("sem tempo aqui", None),
    ],
)
def test_extract_duration(text, expected):
    assert extract_duration(text)[1] == expected


def test_extract_log_args():
    remaining, args = extract_log_args(normalize("log de erros das últimas 2 horas"))
    assert args == ["2h", "erros"]
    assert "2" not in remaining


@pytest.mark.parametrize(
    ("text", "command"),
    [
        ("ping", "ping"),
        ("Ping!", "ping"),
        ("log 2 horas", "log"),
        ("me mostra o log", "log"),
        ("ajuda", "help"),
        ("status", "status"),
        ("staus", "status"),
    ],
)
def test_high_scores(text, command):
    match = best_match(text, build_registry())
    assert match is not None
    assert match.command.name == command
    assert match.score >= 90


@pytest.mark.parametrize(
    "text",
    [
        "qual a capital da França",
        "pesquisa sobre o status da economia",
        "me lembra de comprar pão amanhã",
    ],
)
def test_unrelated_text_does_not_match(text):
    match = best_match(text, build_registry())
    assert match is None or match.score < 75


@pytest.fixture
def router(services) -> Router:
    return Router(services)


async def test_ping_free_text_runs_command(router):
    assert (await router.handle_text("ping")).text == "pong"


async def test_log_free_text_gets_arguments(router, event_store):
    await router.handle_text("log 2 horas")
    command_events = event_store.of_type(EventType.COMMAND)
    assert command_events[-1].data == {"name": "log", "args": ["2h"]}
    route = event_store.of_type(EventType.ROUTE)[-1]
    assert route.data["tier"] == "similarity" and route.data["score"] >= 90


async def test_capital_of_france_is_not_a_command(router, event_store):
    reply = await router.handle_text("qual a capital da França")
    assert reply.text == LLM_DISABLED
    assert event_store.of_type(EventType.COMMAND) == []


async def test_medium_score_asks_for_confirmation(router, services):
    services.settings.similarity_execute_threshold = 101  # force the medium band
    reply = await router.handle_text("ping")
    assert reply.text == "Você quis dizer /ping?"
    assert [b.text for b in reply.buttons] == ["Sim", "Não"]

    confirmed = await router.handle_callback(reply.buttons[0].data)
    assert confirmed.text == "pong"
    # The token is single-use.
    again = await router.handle_callback(reply.buttons[0].data)
    assert "expirou" in again.text


async def test_confirmation_can_be_denied(router, services):
    services.settings.similarity_execute_threshold = 101
    reply = await router.handle_text("ping")
    denied = await router.handle_callback(reply.buttons[1].data)
    assert denied.text == CONFIRM_DENIED


async def test_confirmation_keeps_the_original_trace(router, services, event_store):
    services.settings.similarity_execute_threshold = 101
    reply = await router.handle_text("ping")
    first_trace = event_store.events[0].trace_id
    await router.handle_callback(reply.buttons[0].data)
    assert {e.trace_id for e in event_store.events} == {first_trace}
