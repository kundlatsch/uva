import pytest

from uva.commands import Command, CommandRegistry, build_registry
from uva.router import NOT_UNDERSTOOD, Router


@pytest.fixture
def router() -> Router:
    return Router(build_registry())


async def test_ping_returns_pong(router):
    assert await router.handle_text("/ping") == "pong"


async def test_command_with_bot_suffix(router):
    assert await router.handle_text("/ping@UvaBot") == "pong"


async def test_command_is_case_insensitive(router):
    assert await router.handle_text("/PING") == "pong"


async def test_alias_resolves_to_command(router):
    assert await router.handle_text("/ajuda") == await router.handle_text("/help")


async def test_help_lists_every_command(router):
    text = await router.handle_text("/help")
    for cmd in router.registry:
        assert f"/{cmd.name}" in text
        assert cmd.description in text


async def test_unknown_command(router):
    assert "desconhecido" in await router.handle_text("/nope")


async def test_free_text_falls_back(router):
    assert await router.handle_text("bom dia") == NOT_UNDERSTOOD


def test_registry_has_builtin_commands():
    names = {cmd.name for cmd in build_registry()}
    assert {"start", "help", "ping"} <= names


def test_duplicate_alias_is_rejected():
    async def noop(ctx):
        return ""

    registry = CommandRegistry()
    registry.register(Command("a", "first", noop, aliases=("x",)))
    with pytest.raises(ValueError):
        registry.register(Command("b", "second", noop, aliases=("X",)))


def test_iteration_skips_alias_duplicates():
    registry = build_registry()
    assert len(list(registry)) == len(registry) == 3
