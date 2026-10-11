"""Command registry: every bot command is a plain async function plus metadata."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CommandContext:
    """What a command receives. Kept Telegram-agnostic so it is easy to test."""

    args: list[str]
    registry: CommandRegistry
    user_id: int | None = None


CommandHandler = Callable[[CommandContext], Awaitable[str]]


@dataclass(frozen=True, slots=True)
class Command:
    name: str
    description: str
    handler: CommandHandler
    # Alternative names. Exact aliases work now; later phases will also use
    # them for similarity matching of free text.
    aliases: tuple[str, ...] = ()

    @property
    def all_names(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)


class CommandRegistry:
    def __init__(self) -> None:
        self._commands: dict[str, Command] = {}
        self._lookup: dict[str, Command] = {}

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
        self, name: str, description: str, aliases: tuple[str, ...] = ()
    ) -> Callable[[CommandHandler], CommandHandler]:
        """Decorator form of :meth:`register`."""

        def decorator(func: CommandHandler) -> CommandHandler:
            self.register(Command(name, description, func, tuple(aliases)))
            return func

        return decorator

    def get(self, name_or_alias: str) -> Command | None:
        return self._lookup.get(_normalize(name_or_alias))

    def __iter__(self) -> Iterator[Command]:
        """Primary commands only (no alias duplicates), in registration order."""
        return iter(self._commands.values())

    def __len__(self) -> int:
        return len(self._commands)


def _normalize(name: str) -> str:
    return name.strip().lstrip("/").lower()
