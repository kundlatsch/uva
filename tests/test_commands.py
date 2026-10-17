import pytest

from uva.commands import Command, CommandRegistry, build_registry
from uva.llm.agent import LLM_DISABLED
from uva.router import Router


@pytest.fixture
def router(services) -> Router:
    return Router(services)


async def text_of(router: Router, message: str) -> str:
    return (await router.handle_text(message)).text


async def test_ping_returns_pong(router):
    assert await text_of(router, "/ping") == "pong"


async def test_command_with_bot_suffix(router):
    assert await text_of(router, "/ping@UvaBot") == "pong"


async def test_command_is_case_insensitive(router):
    assert await text_of(router, "/PING") == "pong"


async def test_alias_resolves_to_command(router):
    assert await text_of(router, "/ajuda") == await text_of(router, "/help")


# Every command answers to a Portuguese and an English name.
BILINGUAL = {
    "start": "inicio", "help": "ajuda", "brain": "cerebro", "nota": "note", "buscar": "search",
    "memoria": "memory", "lembrar": "remember", "esquecer": "forget", "figurinhas": "stickers",
    "figurinha": "sticker", "contexto": "context", "objetivos": "goals", "pausar": "pause",
    "retomar": "resume", "remover": "remove", "rotina": "routine", "rotinas": "routines",
    "proativo": "proactive", "gasto": "expense", "gastos": "expenses", "decidir": "decide",
    "log": "eventos", "status": "estado", "llm": "model", "liberar": "unlock", "soul": "alma",
    "frases": "phrases", "reverter": "undo", "feature": "funcionalidade",
    "features": "funcionalidades", "projeto": "project", "projetos": "projects", "sh": "terminal",
    "py": "python", "tarefas": "tasks", "cancelar": "cancel", "comandos": "commands",
}


@pytest.mark.parametrize(("name", "other"), BILINGUAL.items())
def test_commands_have_portuguese_and_english_names(name, other):
    registry = build_registry()
    assert registry.get(name) is registry.get(other) is not None


async def test_help_lists_every_command(router):
    text = await text_of(router, "/help")
    for cmd in router.registry:
        assert f"/{cmd.name}" in text
        assert cmd.description in text


async def test_unknown_command_gets_one_message(router):
    text = await text_of(router, "/nope")
    assert "/nope" in text and "\n" not in text  # one phrase, not phrase + fixed text
    assert text in [p.replace("{comando}", "nope") for p in router.services.phrases.categories["comando_desconhecido"]]


async def test_free_text_falls_back(router):
    text = await text_of(router, "qual a capital da França")
    first, second = text.split("\n")
    assert first in router.services.phrases.categories["nao_entendi"]
    assert second == LLM_DISABLED


def test_registry_has_builtin_commands():
    names = {cmd.name for cmd in build_registry()}
    assert {"start", "help", "ping", "log", "status"} <= names


def test_duplicate_alias_is_rejected():
    async def noop(ctx):
        return ""

    registry = CommandRegistry()
    registry.register(Command("a", "first", noop, aliases=("x",)))
    with pytest.raises(ValueError):
        registry.register(Command("b", "second", noop, aliases=("X",)))


def test_iteration_skips_alias_duplicates():
    registry = build_registry()
    assert len(list(registry)) == len(registry)
    assert len({c.name for c in registry}) == len(registry)


async def test_catalog_lists_commands_and_actions(router):
    reply = await router.handle_text("/comandos")
    text = reply.document.decode() if reply.document else reply.text
    assert "/ping [core]" in text
    assert "apelidos: ajuda" in text
    assert "- search [proativa, tarefa]" in text
    assert "params: query: string, title_pt: string, source: string?" in text
    assert "- run_command:" in text


async def test_help_and_catalog_in_english():
    from fakes import make_services

    from uva import i18n

    i18n.set_language("en")
    router = Router(make_services())  # descriptions are read when commands register
    help_text = await text_of(router, "/help")
    assert help_text.startswith("Available commands:")
    assert "/ping - Checks whether the bot is alive" in help_text
    assert "(also: ajuda)" in help_text
    assert (await text_of(router, "/start")).startswith("Hi! I'm UVA")

    reply = await router.handle_text("/commands")
    text = reply.document.decode() if reply.document else reply.text
    assert text.startswith("# Commands")
    assert "aliases: ajuda" in text and "# LLM actions" in text
    assert "- search [proactive, task]" in text
    assert reply.filename in (None, "commands.md")
