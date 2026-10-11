"""Turns an incoming text message into a reply.

Routing tiers:
  1. Explicit commands (``/name args``), resolved by name or alias.
  2. TODO (phase 2): similarity match of free text against command aliases.
  3. TODO (phase 3): LLM fallback (intent classification / conversation).
Anything that falls through every tier gets a "not understood" reply.
"""

from __future__ import annotations

from uva.commands import CommandContext, CommandRegistry
from uva.logging import get_logger

log = get_logger(__name__)

NOT_UNDERSTOOD = "Não entendi. Envie /help para ver o que eu sei fazer."
UNKNOWN_COMMAND = "Comando desconhecido: /{name}. Envie /help para ver a lista."


class Router:
    def __init__(self, registry: CommandRegistry) -> None:
        self.registry = registry

    async def handle_text(self, text: str, user_id: int | None = None) -> str:
        text = text.strip()

        # Tier 1: explicit command.
        if text.startswith("/"):
            return await self._dispatch_command(text, user_id)

        # Tier 2: TODO - fuzzy match `text` against command names/aliases.
        # Tier 3: TODO - hand off to the LLM (see uva.llm), within budget.

        log.info("text_not_understood", length=len(text))
        return NOT_UNDERSTOOD

    async def _dispatch_command(self, text: str, user_id: int | None) -> str:
        head, *args = text.split()
        # "/ping@MyBot" -> "ping" (Telegram appends the bot name in some clients).
        name = head[1:].split("@", 1)[0]

        command = self.registry.get(name)
        if command is None:
            log.info("unknown_command", command=name)
            return UNKNOWN_COMMAND.format(name=name)

        log.info("command_dispatched", command=command.name, invoked_as=name)
        ctx = CommandContext(args=args, registry=self.registry, user_id=user_id)
        return await command.handler(ctx)
