"""Command registry: every bot command is a plain async function plus metadata."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

from uva.replies import Reply

if TYPE_CHECKING:
    from uva.services import Services


@dataclass(frozen=True, slots=True)
class CommandContext:
    """What a command receives. Kept Telegram-agnostic so it is easy to test."""

    args: list[str]
    services: Services
    user_id: int | None = None
    # Everything after the command name, as typed (newlines kept), e.g. for /py.
    raw_args: str = ""
    # Sends a request to the LLM agent: ask(text, skill=...) (set by the router).
    ask: Callable[..., Awaitable[Reply]] | None = None
    # Routes a text as if the user had typed it again (set by the router; /refazer).
    reroute: Callable[[str], Awaitable[Reply]] | None = None

    @property
    def registry(self) -> CommandRegistry:
        return self.services.registry


CommandHandler = Callable[[CommandContext], Awaitable[str | Reply]]
# Receives normalized free text, returns (text without the arguments, args).
ArgExtractor = Callable[[str], tuple[str, list[str]]]


@dataclass(frozen=True, slots=True)
class Command:
    name: str
    description: str
    handler: CommandHandler
    # Alternative names. Exact aliases work as commands; aliases and examples
    # are also used for similarity matching of free text.
    aliases: tuple[str, ...] = ()
    # Usage hint shown in /help, e.g. "<período> [tipo]".
    usage: str = ""
    # Example phrases for similarity matching ("o que aconteceu" -> /log).
    examples: tuple[str, ...] = ()
    extract_args: ArgExtractor | None = None
    # "core" for built-in commands, "plugin" for ones loaded from uva/plugins.
    source: str = "core"

    @property
    def all_names(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)


class CommandRegistry:
    def __init__(self) -> None:
        self._commands: dict[str, Command] = {}
        self._lookup: dict[str, Command] = {}
        # Plugins unused for PLUGIN_STALE_DAYS (uva/curator.py): out of /help and
        # the prompt, still callable.
        self.idle: set[str] = set()

    def register(self, command: Command) -> Command:
        keys = [_normalize(n) for n in command.all_names]
        for key in keys:
            if key in self._lookup:
                raise ValueError(f"Command name or alias already registered: {key!r}")
        self._commands[keys[0]] = command
        for key in keys:
            self._lookup[key] = command
        return command

    def command(
        self,
        name: str,
        description: str,
        aliases: tuple[str, ...] = (),
        usage: str = "",
        examples: tuple[str, ...] = (),
        extract_args: ArgExtractor | None = None,
    ) -> Callable[[CommandHandler], CommandHandler]:
        """Decorator form of :meth:`register`."""

        def decorator(func: CommandHandler) -> CommandHandler:
            self.register(
                Command(name, description, func, aliases, usage, examples, extract_args)
            )
            return func

        return decorator

    def remove_source(self, source: str) -> None:
        """Drop every command of a source (used to reload plugins)."""
        self._commands = {k: c for k, c in self._commands.items() if c.source != source}
        self._lookup = {k: c for k, c in self._lookup.items() if c.source != source}

    def get(self, name_or_alias: str) -> Command | None:
        return self._lookup.get(_normalize(name_or_alias))

    def __iter__(self) -> Iterator[Command]:
        """Primary commands only (no alias duplicates), in registration order."""
        return iter(self._commands.values())

    def __len__(self) -> int:
        return len(self._commands)


def _normalize(name: str) -> str:
    return name.strip().lstrip("/").lower()
