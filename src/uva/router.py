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
from uva.llm.base import LLMError, LLMMessage
from uva.media import current_photo, mentions_image
from uva.stm.refs import remember_ref
from uva.text import normalize
from uva.matching import best_match, small_talk, status_question
from uva.replies import Button, Reply
from uva.services import Services
from uva.audio import TranscriptionError
from uva.llm.budget import Scope
from uva.stm.status import refresh, status_reply

UNKNOWN_COMMAND = "/{name} não existe. Envie /help para ver a lista."
CONFIRM_EXPIRED = "Essa pergunta expirou. Pode mandar de novo?"

CONFIRM_TTL = timedelta(minutes=10)
PAGES_TTL = timedelta(hours=1)
PAGES_KEPT = 20
PAGES_EXPIRED = "Essa paginação expirou. Rode o comando de novo."
HEARD_CHARS = 800
DESCRIBE_PROMPT = (
    "Descreva esta imagem em português do Brasil, em 1 a 3 frases objetivas. Se houver "
    "texto legível, transcreva-o entre aspas (no idioma original)."
)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


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
        # Paginated replies by token: (pages, created at).
        self._pages: dict[str, tuple[list[str], datetime]] = {}
        self.agent = Agent(services, run_command=self._run_command_for_llm)

    @property
    def registry(self) -> CommandRegistry:
        return self.services.registry

    async def handle_text(
        self, text: str, user_id: int | None = None, photo: str | None = None
    ) -> Reply:
        """Route a message; ``photo`` is a data URL when it came with an image."""
        await self._mark_busy()
        if photo is not None:
            self.services.last_photo.set(photo)
        token = current_photo.set(photo)
        try:
            with trace():
                await emit(EventType.MESSAGE_RECEIVED, text=text, photo=photo is not None)
                reply = await self._safely(self._route(text.strip(), user_id, photo))
                await self._remember(text + (" [foto]" if photo else ""), reply)
                return self._first_page(reply)
        finally:
            current_photo.reset(token)

    async def handle_photo(
        self, photo: str, caption: str = "", user_id: int | None = None
    ) -> Reply:
        """A picture: with a caption it's a message with an image; without one,
        the bot describes it and keeps it for follow-ups ("o que tá escrito aí?")."""
        if caption.strip():
            return await self.handle_text(caption, user_id=user_id, photo=photo)

        await self._mark_busy()
        services = self.services
        services.last_photo.set(photo)
        with trace():
            await emit(EventType.MESSAGE_RECEIVED, text="[foto]", photo=True)
            reply = await self._safely(self._describe_photo(photo))
            await self._remember("[foto sem legenda]", reply)
            return reply

    async def _describe_photo(self, photo: str) -> Reply:
        services = self.services
        llm = services.llm
        if llm is None:
            return Reply("📷 Guardei a foto. Manda /decidir ... ou pergunta algo sobre ela.")
        if services.budget is not None:
            status = await services.budget.check(Scope.INTERACTIVE)
            if not status.allowed:
                return Reply(f"📷 Guardei a foto, mas não posso olhar agora.\n{services.say('sem_credito')}\n({status.reason})")
        try:
            response = await llm.complete(
                [LLMMessage("user", DESCRIBE_PROMPT, images=(photo,))],
                purpose="vision_describe",
                scope=Scope.INTERACTIVE,
                model=llm.vision_model,
                max_tokens=600,
                use_soul=False,  # a factual description; the follow-ups get the personality
            )
        except LLMError:
            return Reply(f"📷 Guardei a foto, mas não consegui olhar.\n{services.say('llm_erro')}")
        description = response.text.strip() or "(não consegui descrever)"
        await emit(EventType.ROUTE, tier="vision_describe", chars=len(description))
        await remember_ref(services, "imagem", utcnow().strftime("%H%M%S"), f"imagem: {_clip(description, 80)}")
        return Reply(f"📷 {description}\n\n(Pergunta o que quiser sobre ela, ou pede para eu salvar.)")

    async def _followup_image(self, text: str) -> str | None:
        """The last picture, when this message seems to be about it."""
        photo = self.services.last_photo.get()
        if photo is None:
            return None
        if mentions_image(normalize(text)):
            return photo
        if self.services.stm is not None:
            try:
                focused = (await self.services.stm.session()).focused()
            except Exception:
                return None
            if focused is not None and focused.tipo == "imagem":
                return photo
        return None

    async def handle_audio(
        self, data: bytes, *, seconds: float | None = None, user_id: int | None = None
    ) -> list[Reply]:
        """Voice note/audio: transcribe, echo what was understood, then route
        the transcript exactly as if it had been typed."""
        await self._mark_busy()
        services = self.services
        with trace():
            await emit(EventType.MESSAGE_RECEIVED, text="[áudio]", kind="voice", seconds=seconds)
            if services.transcriber is None:
                text = "Ainda não sei ouvir áudio: falta uma chave da OpenAI (TRANSCRIBE_API_KEY)."
                return [Reply(text)]
            if services.budget is not None:
                status = await services.budget.check(Scope.INTERACTIVE)
                if not status.allowed:
                    return [Reply(f"{services.say('sem_credito')}\n({status.reason})")]
            try:
                transcript = await services.transcriber.transcribe(data, seconds=seconds)
            except TranscriptionError as exc:
                return [Reply(f"{services.say('llm_erro')}\n(transcrição: {exc})")]
            if not transcript.text:
                return [Reply(f"{services.say('nao_entendi')}\n(não consegui ouvir nada no áudio)")]

            await emit(EventType.ROUTE, tier="voice", chars=len(transcript.text), model=transcript.model)
            heard = Reply(f"🎙️ entendi: {_clip(transcript.text, HEARD_CHARS)}")
            reply = await self._safely(self._route(transcript.text.strip(), user_id))
            if services.budget is not None and not (await services.budget.check(Scope.INTERACTIVE)).allowed:
                reply.text += "\n(guardei a transcrição inteira; depois do /liberar é só pedir de novo)"
            await self._remember(f"[voz] {transcript.text}", reply)
            return [heard, reply]

    async def _remember(self, text: str, reply: Reply) -> None:
        """Keep the exchange in the short-term memory (best effort)."""
        if self.services.stm is None:
            return
        try:
            await self.services.stm.add_exchange(text, reply.summary or reply.text)
        except Exception as exc:
            await emit(EventType.ERROR, where="stm", error=repr(exc))

    def _first_page(self, reply: Reply) -> Reply:
        """Keep the pages of a long reply and show the first one with ◀ ▶."""
        if len(reply.pages) <= 1:
            return reply
        now = utcnow()
        self._pages = {
            t: v for t, v in self._pages.items() if now - v[1] < PAGES_TTL
        }
        while len(self._pages) >= PAGES_KEPT:
            self._pages.pop(next(iter(self._pages)))
        token = secrets.token_hex(4)
        self._pages[token] = (reply.pages, now)
        return self._page(token, 0)

    def _page(self, token: str, index: int) -> Reply:
        stored = self._pages.get(token)
        if stored is None:
            return Reply(PAGES_EXPIRED, edit=True)
        pages = stored[0]
        index = max(0, min(index, len(pages) - 1))
        buttons = []
        if index > 0:
            buttons.append(Button("◀ Anterior", f"pg:{token}:{index - 1}"))
        if index < len(pages) - 1:
            buttons.append(Button("Próxima ▶", f"pg:{token}:{index + 1}"))
        text = f"{pages[index]}\n\n— página {index + 1}/{len(pages)} —"
        return Reply(text=text, buttons=buttons, edit=True)

    async def handle_callback(self, data: str, user_id: int | None = None) -> Reply:
        """Answer to an inline button ("confirm:<token>", "deny:<token>", "pg:<token>:<n>")."""
        verb, _, token = data.partition(":")
        if verb == "pg":
            token, _, index = token.partition(":")
            return self._page(token, int(index) if index.isdigit() else 0)
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

    async def _route(self, text: str, user_id: int | None, photo: str | None = None) -> Reply:
        # Tier 1: explicit command (captions too: "/decidir ..." uses the photo).
        if text.startswith("/"):
            return await self._dispatch_command(text, user_id)

        # A message with a picture goes straight to the (vision) LLM.
        if photo is not None:
            return await self.agent.handle(text, image=photo)

        # Small talk ("oi", "valeu", "falou") is answered without the LLM.
        if category := small_talk(text):
            await emit(EventType.ROUTE, tier="small_talk", category=category)
            return Reply(self.services.say(category))

        # "o que tá escrito aí?" right after a picture: show it to the LLM again.
        if (image := await self._followup_image(text)) is not None:
            await emit(EventType.ROUTE, tier="image_followup")
            return await self.agent.handle(text, image=image)

        # "como tá aquela pesquisa?" -> status of the ref in focus, without the LLM.
        if reply := await self._try_status_question(text):
            return reply

        # Tier 2: similarity against command names, aliases and examples.
        if reply := await self._try_similarity(text, user_id):
            return reply

        # Tier 3: the LLM (within budget).
        return await self.agent.handle(text)

    async def _try_status_question(self, text: str) -> Reply | None:
        question = status_question(text)
        if question is None or self.services.stm is None:
            return None
        try:
            session = await self.services.stm.session()
        except Exception:
            return None
        ref = session.find(tipo=question.kind)
        if ref is None:
            return None  # nothing to talk about: let the LLM handle it
        ref, live = await refresh(self.services, ref)
        await emit(EventType.ROUTE, tier="status", ref=ref.ref, kind=ref.tipo, status=live.status)
        return Reply(status_reply(self.services, ref, live))

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
        reply = Reply.coerce(await command.handler(ctx))
        if reply.summary is None:
            first_line = reply.text.strip().splitlines()[0] if reply.text.strip() else ""
            invoked = " ".join([f"/{command.name}", *args])
            reply.summary = f"{invoked} → {first_line[:150]}"
        return reply

