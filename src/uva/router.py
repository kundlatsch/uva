"""Turns an incoming text message into a reply.

Routing tiers:
  1. Explicit commands (``/name args``), resolved by name or alias.
  2. Similarity match of free text against command names, aliases and
     examples: a high score runs the command, a medium score asks first.
  3. The LLM, which answers with JSON actions (see uva.llm.agent).

Every message runs under its own trace id; each step is recorded as an event.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from uva.clock import utcnow
from uva.commands import CommandContext, CommandRegistry
from uva.llm.agent import Agent
from uva.logging.events import EventType, current_trace_id, emit, new_trace_id, trace
from uva.matching import best_match, small_talk
from uva.replies import Button, Reply
from uva.services import Services

UNKNOWN_COMMAND = "/{name} não existe. Envie /help para ver a lista."
CONFIRM_EXPIRED = "Essa pergunta expirou. Pode mandar de novo?"

CONFIRM_TTL = timedelta(minutes=10)


@dataclass(slots=True)
class PendingCommand:
    name: str
    args: list[str]
    trace_id: str
    expires_at: datetime = field(default_factory=lambda: utcnow() + CONFIRM_TTL)


class Router:
    def __init__(self, services: Services) -> None:
        self.services = services
        # Tier-2 questions waiting for a Sim/Não button, by token.
        self._pending: dict[str, PendingCommand] = {}
        self.agent = Agent(services, run_command=self._run_command_for_llm)

    @property
    def registry(self) -> CommandRegistry:
        return self.services.registry

    async def handle_text(self, text: str, user_id: int | None = None) -> Reply:
        await self._mark_busy()
        with trace():
            await emit(EventType.MESSAGE_RECEIVED, text=text)
            return await self._safely(self._route(text.strip(), user_id))

    async def handle_callback(self, data: str, user_id: int | None = None) -> Reply:
        """Answer to an inline button ("confirm:<token>" / "deny:<token>")."""
        verb, _, token = data.partition(":")
        pending = self._pending.pop(token, None)
        with trace(pending.trace_id if pending else None):
            await emit(EventType.MESSAGE_RECEIVED, text=f"[botão] {verb}", kind="callback")
            return await self._safely(self._resolve_confirmation(verb, pending, user_id))

    async def _mark_busy(self) -> None:
        """Tell the goal loop the user is around (it waits until we're idle)."""
        if self.services.busy is None:
            return
        try:
            await self.services.busy.mark("chat", self.services.settings.busy_after_message_seconds)
        except Exception:
            pass  # Redis down must not block the conversation

    async def _safely(self, coro) -> Reply:
        try:
            reply = await coro
        except Exception as exc:
            await emit(EventType.ERROR, where="router", error=repr(exc))
            reply = Reply(f"{self.services.say('erro_generico')}\n(registrado em /log erros)")
        await emit(EventType.REPLY, length=len(reply.text), file=reply.document is not None)
        return reply

    async def _route(self, text: str, user_id: int | None) -> Reply:
        # Tier 1: explicit command.
        if text.startswith("/"):
            return await self._dispatch_command(text, user_id)

        # Small talk ("oi", "valeu", "falou") is answered without the LLM.
        if category := small_talk(text):
            await emit(EventType.ROUTE, tier="small_talk", category=category)
            return Reply(self.services.say(category))

        # Tier 2: similarity against command names, aliases and examples.
        if reply := await self._try_similarity(text, user_id):
            return reply

        # Tier 3: the LLM (within budget).
        return await self.agent.handle(text)

    async def _dispatch_command(self, text: str, user_id: int | None) -> Reply:
        head, *args = text.split()
        # "/ping@MyBot" -> "ping" (Telegram appends the bot name in some clients).
        name = head[1:].split("@", 1)[0]

        command = self.registry.get(name)
        if command is None:
            await emit(EventType.ROUTE, tier="unknown_command", command=name)
            text = f"{self.services.say('nao_entendi')}\n{UNKNOWN_COMMAND.format(name=name)}"
            return Reply(text)

        await emit(EventType.ROUTE, tier="command", command=command.name, invoked_as=name)
        return await self.run_command(command.name, args, user_id)

    async def _try_similarity(self, text: str, user_id: int | None) -> Reply | None:
        settings = self.services.settings
        match = best_match(text, self.registry)
        if match is None:
            return None

        decision = {
            "command": match.command.name,
            "score": round(match.score, 1),
            "args": match.args,
            "phrase": match.phrase,
        }
        if match.score >= settings.similarity_execute_threshold:
            await emit(EventType.ROUTE, tier="similarity", **decision)
            return await self.run_command(match.command.name, match.args, user_id)

        if match.score >= settings.similarity_confirm_threshold:
            await emit(EventType.ROUTE, tier="confirm", **decision)
            return self._ask_confirmation(match.command.name, match.args)

        # Below both thresholds: logged so the thresholds can be tuned.
        await emit(EventType.ROUTE, tier="similarity_miss", **decision)
        return None

    def _ask_confirmation(self, name: str, args: list[str]) -> Reply:
        self._expire_pending()
        token = secrets.token_hex(4)
        trace_id = current_trace_id() or new_trace_id()
        self._pending[token] = PendingCommand(name, args, trace_id)
        suggestion = " ".join([f"/{name}", *args])
        return Reply(
            text=f"Você quis dizer {suggestion}?",
            buttons=[Button("Sim", f"confirm:{token}"), Button("Não", f"deny:{token}")],
        )

    async def _resolve_confirmation(
        self, verb: str, pending: PendingCommand | None, user_id: int | None
    ) -> Reply:
        if pending is None or pending.expires_at < utcnow():
            await emit(EventType.ROUTE, tier="confirm_expired")
            return Reply(CONFIRM_EXPIRED)
        if verb != "confirm":
            await emit(EventType.ROUTE, tier="confirm_denied", command=pending.name)
            return Reply(self.services.say("cancelado"))
        await emit(EventType.ROUTE, tier="confirmed", command=pending.name)
        return await self.run_command(pending.name, pending.args, user_id)

    def _expire_pending(self) -> None:
        now = utcnow()
        for token in [t for t, p in self._pending.items() if p.expires_at < now]:
            del self._pending[token]

    async def _run_command_for_llm(self, name: str, args: list[str]) -> Reply:
        return await self.run_command(name, args, self.services.settings.allowed_telegram_user_id)

    async def run_command(self, name: str, args: list[str], user_id: int | None) -> Reply:
        command = self.registry.get(name)
        if command is None:
            raise KeyError(name)
        await emit(EventType.COMMAND, name=command.name, args=args)
        ctx = CommandContext(args=args, services=self.services, user_id=user_id)
        return Reply.coerce(await command.handler(ctx))

