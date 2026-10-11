"""Turns an incoming text message into a reply.

Routing tiers:
  1. Explicit commands (``/name args``), resolved by name or alias.
  2. TODO (phase 3): similarity match of free text against command aliases.
  3. TODO (phase 4): LLM fallback with JSON actions.
Anything that falls through every tier gets a "not understood" reply.

Every message runs under its own trace id; each step is recorded as an event.
"""

from __future__ import annotations

from uva.commands import CommandContext
from uva.logging.events import EventType, emit, trace
from uva.replies import Reply
from uva.services import Services

NOT_UNDERSTOOD = "Não entendi. Envie /help para ver o que eu sei fazer."
UNKNOWN_COMMAND = "Comando desconhecido: /{name}. Envie /help para ver a lista."
INTERNAL_ERROR = "Algo deu errado do meu lado. O erro foi registrado (/log erros)."


class Router:
    def __init__(self, services: Services) -> None:
        self.services = services

    @property
    def registry(self):
        return self.services.registry

    async def handle_text(self, text: str, user_id: int | None = None) -> Reply:
        with trace():
            await emit(EventType.MESSAGE_RECEIVED, text=text)
            try:
                reply = await self._route(text.strip(), user_id)
            except Exception as exc:
                await emit(EventType.ERROR, where="router", error=repr(exc))
                reply = Reply(INTERNAL_ERROR)
            await emit(EventType.REPLY, length=len(reply.text), file=reply.document is not None)
            return reply

    async def _route(self, text: str, user_id: int | None) -> Reply:
        # Tier 1: explicit command.
        if text.startswith("/"):
            return await self._dispatch_command(text, user_id)

        # Tier 2: TODO - fuzzy match `text` against command names/aliases.
        # Tier 3: TODO - hand off to the LLM, within budget.

        await emit(EventType.ROUTE, tier="none")
        return Reply(NOT_UNDERSTOOD)

    async def _dispatch_command(self, text: str, user_id: int | None) -> Reply:
        head, *args = text.split()
        # "/ping@MyBot" -> "ping" (Telegram appends the bot name in some clients).
        name = head[1:].split("@", 1)[0]

        command = self.registry.get(name)
        if command is None:
            await emit(EventType.ROUTE, tier="unknown_command", command=name)
            return Reply(UNKNOWN_COMMAND.format(name=name))

        await emit(EventType.ROUTE, tier="command", command=command.name, invoked_as=name)
        return await self.run_command(command.name, args, user_id)

    async def run_command(self, name: str, args: list[str], user_id: int | None) -> Reply:
        command = self.registry.get(name)
        if command is None:
            raise KeyError(name)
        await emit(EventType.COMMAND, name=command.name, args=args)
        ctx = CommandContext(args=args, services=self.services, user_id=user_id)
        return Reply.coerce(await command.handler(ctx))
