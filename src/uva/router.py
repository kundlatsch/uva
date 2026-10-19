"""Turns an incoming text message into a reply.

Routing tiers:
  1. Explicit commands (``/name args``), resolved by name or alias.
  2. Similarity match of free text against command names, aliases and
     examples: a high score runs the command, a medium score asks first.
  3. The LLM, which answers with JSON actions (see uva.llm.agent).

Every message runs under its own trace id; each step is recorded as an event.
"""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from uva.clock import utcnow
from uva.i18n import language_name, t
from uva.commands import CommandContext, CommandRegistry
from uva.llm.agent import Agent
from uva.logging.events import EventType, current_trace_id, emit, new_trace_id, trace
from uva.llm.base import LLMError, LLMMessage
from uva import documents, provenance, sent
from uva.inbound import FORWARD_ONLY_REQUEST, Composed
from uva.media import current_photo, mentions_image
from uva.safety import as_data, sanitize_untrusted
from uva.stickers import SKIP, StickerFocus, action_message, action_request, as_message
from uva.stm.refs import remember_ref
from uva.text import normalize
from uva.matching import best_match, small_talk, status_question
from uva.replies import Button, Reply
from uva.services import Services
from uva.audio import TranscriptionError
from uva.llm.budget import Scope
from uva.stm.status import refresh, status_reply

CONFIRM_TTL = timedelta(minutes=10)
UNDO_TURN = ("/desfazer_turno", "/undo_turn")
PAGES_TTL = timedelta(hours=1)
PAGES_KEPT = 20
HEARD_CHARS = 800
# A document's text kept in the conversation log (the whole text is in its note).
EXTERNAL_LOG_CHARS = 2000


def describe_prompt(count: int = 1) -> str:
    # The description is shown to the user: written in the configured language.
    what = "this image" if count == 1 else f"these {count} images (one short paragraph each)"
    return (
        f"Describe {what} in {language_name()}, in 1 to 3 objective sentences. If there is "
        "legible text, transcribe it in quotes (in its original language)."
    )


# What the LLM is asked when a document comes without a caption.
DOCUMENT_REQUEST = "(No caption: summarize the document in a few lines and say where it was saved.)"


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
        # The last sticker sent (waiting for its meaning, or target of "toda vez que...").
        self._sticker: StickerFocus | None = None
        self._curated_at: datetime | None = None

    @property
    def registry(self) -> CommandRegistry:
        return self.services.registry

    async def handle_text(
        self, text: str, user_id: int | None = None, photo: str | None = None, skill: str | None = None,
        note: str = "", inbound: Composed | None = None, images: tuple[str, ...] = (),
    ) -> Reply:
        """Route a message; ``photo`` is a data URL when it came with an image
        (``images``: the rest of an album); ``skill`` names a skill to follow and
        ``note`` is extra context for the LLM (both used by routines);
        ``inbound`` is a batch from Telegram (quoted reply, forwards)."""
        await self._mark_busy()
        if photo is not None:
            self.services.last_photo.set(photo)
        token = current_photo.set(photo)
        label = inbound.label if inbound is not None and inbound.label else text
        try:
            with trace():
                await emit(EventType.MESSAGE_RECEIVED, text=label, photo=photo is not None)
                if inbound is not None:
                    await self._focus_quoted(inbound)
                    note = "\n\n".join(x for x in (inbound.context, note) if x)
                if inbound is not None and not text.strip() and inbound.context:
                    # Only forwards: the LLM decides what to do with them.
                    work = self.agent.handle(FORWARD_ONLY_REQUEST, image=photo, images=images, note=note)
                else:
                    work = self._route(text.strip(), user_id, photo, skill, note, images)
                with provenance.incoming("forward" if inbound is not None and inbound.external else ""):
                    reply = await self._safely(work)
                external = inbound.external if inbound is not None else ""
                own = (inbound.text if inbound is not None else text) + (" [photo]" if photo else "")
                await self._remember(label + (" [photo]" if photo else ""), reply, logged=own, external=external)
                return self._first_page(reply)
        finally:
            current_photo.reset(token)

    async def _focus_quoted(self, inbound: Composed) -> None:
        """A reply quoting one of UVA's notices: focus moves to what it was about."""
        quote, services = inbound.quoted, self.services
        if quote is None or not quote.from_bot or services.stm is None or services.sqlite is None:
            return
        try:
            found = await sent.lookup(services.sqlite, quote.message_id)
            if found is None or not found.ref:
                return
            session = await services.stm.session()
            ref = next((r for r in reversed(session.refs) if r.id == found.ref), None)
            if ref is not None:
                await services.stm.set_focus(ref.ref)
                await emit(EventType.ROUTE, tier="quote_focus", ref=ref.ref, kind=found.kind)
        except Exception as exc:
            await emit(EventType.ERROR, where="quote_focus", error=repr(exc))

    async def handle_photo(
        self, photo: str, caption: str = "", user_id: int | None = None, images: tuple[str, ...] = (),
        inbound: Composed | None = None,
    ) -> Reply:
        """A picture (``images``: the rest of an album): with a caption it's a
        message with an image; without one, the bot describes it and keeps it for
        follow-ups ("o que tá escrito aí?")."""
        if caption.strip() or (inbound is not None and inbound.context):
            return await self.handle_text(caption, user_id=user_id, photo=photo, images=images, inbound=inbound)

        await self._mark_busy()
        services = self.services
        services.last_photo.set(photo)
        with trace():
            await emit(EventType.MESSAGE_RECEIVED, text="[photo]", photo=True, album=len(images) + 1)
            reply = await self._safely(self._describe_photo(photo, images))
            label = "[photo without caption]" if not images else f"[album of {len(images) + 1} photos]"
            await self._remember(label, reply)
            return reply

    async def handle_document(
        self, filename: str, data: bytes, caption: str = "", user_id: int | None = None,
        inbound: Composed | None = None,
    ) -> Reply:
        """A document (PDF, DOCX, TXT...): kept in the brain's 05-anexos with its
        text in a note; the caption (or "summarize it") goes to the router with
        the first part of the text."""
        await self._mark_busy()
        label = f"[document {filename}] {caption}".strip()
        with trace():
            await emit(EventType.MESSAGE_RECEIVED, text=label, kind="document")
            external: list[str] = []
            with provenance.incoming("document"):
                reply = await self._safely(self._document(filename, data, caption, user_id, inbound, external))
            await self._remember(label, reply, external="\n\n".join(external))
            return self._first_page(reply)

    async def _document(
        self, filename: str, data: bytes, caption: str, user_id: int | None, inbound: Composed | None,
        external: list[str] | None = None,
    ) -> Reply:
        services = self.services
        settings = services.settings
        if services.vault is None:
            return Reply(t("router.document.no_vault"))
        try:
            extracted = await asyncio.to_thread(
                documents.extract, filename, data, max_chars=settings.document_max_chars
            )
        except documents.DocumentError as exc:
            await emit(EventType.ROUTE, tier="document_refused", file=filename, error=str(exc))
            return Reply(str(exc))
        attachment, note = await asyncio.to_thread(self._save_document, filename, data, extracted)
        await remember_ref(services, "nota", note.path, f"document: {filename}")
        await emit(EventType.ROUTE, tier="document", file=filename, chars=len(extracted.text),
                   truncated=extracted.truncated, note=note.path)
        if external is not None:  # the document's text goes to the log as outside content
            external.append(f"Document {filename} ({note.path}):\n{extracted.text[:EXTERNAL_LOG_CHARS]}")
        shown, _ = sanitize_untrusted(extracted.text[: settings.document_context_chars])
        pages = f"{extracted.pages} pages, " if extracted.pages else ""
        clipped = f", clipped at {settings.document_max_chars}" if extracted.truncated else ""
        context = (
            f"Document the user just sent: {filename} ({pages}{len(extracted.text)} chars{clipped}). "
            f"Saved as {attachment}, with its text in the note {note.path}.\n"
            + as_data("document", shown)
        )
        if len(shown) < len(extracted.text):
            context += f'\n(The rest: document_read with "path": "{note.path}" and "offset": {len(shown)}.)'
        if inbound is not None and inbound.context:
            context = f"{inbound.context}\n\n{context}"
        if caption.strip():
            return await self._route(caption.strip(), user_id, note=context)
        return await self.agent.handle(DOCUMENT_REQUEST, note=context)

    def _save_document(self, filename: str, data: bytes, extracted: documents.Extracted):
        vault = self.services.vault
        day = utcnow().astimezone(self.services.tz).strftime("%Y-%m-%d")
        stem, _, ext = filename.rpartition(".") if "." in filename else (filename, "", "bin")
        attachment = vault.save_attachment(data, ext.lower() or "bin", f"{day} {stem}")
        note = vault.create_note(
            f"{day} {filename}", documents.note_body(attachment, extracted), folder="anexos",
            tags=["documento"], check=False,
        )
        return attachment, note

    async def handle_sticker(
        self, unique_id: str, *, file_id: str = "", emoji: str = "", set_name: str = "",
        user_id: int | None = None,
    ) -> Reply:
        """A sticker: known ones are understood by their meaning (no vision) or run
        their action; a new one makes the bot ask what it means."""
        await self._mark_busy()
        with trace():
            await emit(EventType.MESSAGE_RECEIVED, text=f"[sticker {emoji}]", kind="sticker", sticker=unique_id)
            reply, label = await self._sticker_reply(unique_id, file_id, emoji, set_name, user_id)
            await self._remember(label, reply)
            return self._first_page(reply)

    async def _sticker_reply(
        self, unique_id: str, file_id: str, emoji: str, set_name: str, user_id: int | None
    ) -> tuple[Reply, str]:
        repo = self.services.stickers
        if repo is None:
            return Reply(t("router.sticker.no_storage", emoji=emoji or "🙂")), f"[sticker {emoji}]"
        try:
            sticker = await repo.get(unique_id)
            if sticker is None:
                sticker = await repo.upsert(unique_id, file_id=file_id, emoji=emoji, set_name=set_name)
            await repo.used(unique_id)
        except Exception as exc:
            await emit(EventType.ERROR, where="stickers", error=repr(exc))
            return Reply(self.services.say("erro_generico")), f"[sticker {emoji}]"
        self._sticker = StickerFocus(unique_id, sticker.emoji or emoji, utcnow(), asking=not sticker.meaning)
        if sticker.action:
            await emit(EventType.ROUTE, tier="sticker_action", sticker=sticker.id, action=sticker.action)
            reply = await self._safely(self._route(action_message(sticker), user_id))
            return reply, f"{as_message(sticker) if sticker.meaning else f'[sticker {sticker.emoji}]'} → {sticker.action}"
        if sticker.meaning:
            await emit(EventType.ROUTE, tier="sticker", sticker=sticker.id)
            reply = await self._safely(self.agent.handle(as_message(sticker)))
            return reply, as_message(sticker)
        await emit(EventType.ROUTE, tier="sticker_new", sticker=sticker.id)
        reply = Reply(t("router.sticker.new", emoji=sticker.emoji))
        return reply, f"[new sticker {sticker.emoji}]"

    async def _try_sticker_text(self, text: str) -> Reply | None:
        """The meaning of the sticker just asked about, or "toda vez que eu mandar
        essa figurinha, ..." for the sticker in focus."""
        focus, repo = self._sticker, self.services.stickers
        if focus is None or repo is None or not focus.fresh():
            return None
        action = action_request(text)
        if action is not None:
            sticker = await repo.upsert(focus.unique_id, action=action)
            self._sticker = None
            await emit(EventType.ROUTE, tier="sticker_learn", sticker=sticker.id, field="action")
            return Reply(t("router.sticker.action_set", emoji=sticker.emoji, action=action, id=sticker.id))
        if not focus.asking:
            return None
        self._sticker = None
        if text.strip().lower() in SKIP or text.startswith("/"):
            if text.startswith("/") and text.strip().lower() not in SKIP:
                return None  # another command: just forget the question
            return Reply(t("router.sticker.skipped"))
        sticker = await repo.upsert(focus.unique_id, meaning=text.strip())
        await emit(EventType.ROUTE, tier="sticker_learn", sticker=sticker.id, field="meaning")
        return Reply(t("router.sticker.learned", emoji=sticker.emoji, meaning=sticker.meaning))

    async def _describe_photo(self, photo: str, images: tuple[str, ...] = ()) -> Reply:
        services = self.services
        llm = services.llm
        if llm is None:
            return Reply(t("router.photo.kept_no_llm"))
        if services.budget is not None:
            status = await services.budget.check(Scope.INTERACTIVE)
            if not status.allowed:
                return Reply(
                    t("router.photo.kept_no_budget", phrase=services.say("sem_credito"), reason=status.reason)
                )
        try:
            response = await llm.complete(
                [LLMMessage("user", describe_prompt(len(images) + 1), images=(photo, *images))],
                purpose="vision_describe",
                scope=Scope.INTERACTIVE,
                model=llm.vision_model,
                max_tokens=600,
                use_soul=False,  # a factual description; the follow-ups get the personality
            )
        except LLMError:
            return Reply(t("router.photo.kept_llm_error", phrase=services.say("llm_erro")))
        description = response.text.strip() or t("router.photo.no_description")
        await emit(EventType.ROUTE, tier="vision_describe", chars=len(description))
        await remember_ref(services, "imagem", utcnow().strftime("%H%M%S"), f"image: {_clip(description, 80)}")
        return Reply(t("router.photo.described", description=description))

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
            await emit(EventType.MESSAGE_RECEIVED, text="[audio]", kind="voice", seconds=seconds)
            if services.transcriber is None:
                return [Reply(t("router.audio.no_transcriber"))]
            if services.budget is not None:
                status = await services.budget.check(Scope.INTERACTIVE)
                if not status.allowed:
                    return [Reply(f"{services.say('sem_credito')}\n({status.reason})")]
            try:
                transcript = await services.transcriber.transcribe(data, seconds=seconds)
            except TranscriptionError as exc:
                phrase = services.say("llm_erro")
                return [Reply(t("router.audio.transcription_failed", phrase=phrase, error=exc))]
            if not transcript.text:
                return [Reply(t("router.audio.nothing_heard", phrase=services.say("nao_entendi")))]

            await emit(EventType.ROUTE, tier="voice", chars=len(transcript.text), model=transcript.model)
            heard = Reply(t("router.audio.heard", text=_clip(transcript.text, HEARD_CHARS)))
            reply = await self._safely(self._route(transcript.text.strip(), user_id))
            if services.budget is not None and not (await services.budget.check(Scope.INTERACTIVE)).allowed:
                reply.text += "\n" + t("router.audio.kept_transcript")
            await self._remember(f"[voice] {transcript.text}", reply)
            return [heard, reply]

    async def _remember(self, text: str, reply: Reply, *, logged: str | None = None, external: str = "") -> None:
        """Keep the exchange in the short-term memory (``text``) and in the
        conversation log (``logged``, his own words, plus ``external``: forwards
        and documents, with origin "external"), best effort."""
        if text.strip().lower().split(" ", 1)[0] in UNDO_TURN:
            return  # undoing the last exchange must not leave a new one behind
        said = reply.summary or reply.text
        index = self.services.index
        if index is not None:
            try:
                trace_id = current_trace_id()
                if external:
                    await index.add_turn("user", external, trace_id=trace_id, origin=provenance.EXTERNAL)
                own = text if logged is None else logged
                if own.strip():
                    await index.add_turn("user", own, trace_id=trace_id)
                await index.add_turn("bot", said, trace_id=trace_id)
            except Exception as exc:
                await emit(EventType.ERROR, where="conversation_log", error=repr(exc))
        if self.services.stm is None:
            return
        try:
            await self.services.stm.add_exchange(text, said)
        except Exception as exc:
            await emit(EventType.ERROR, where="stm", error=repr(exc))

    def _first_page(self, reply: Reply) -> Reply:
        """Keep the pages of a long reply and show the first one with ◀ ▶."""
        if len(reply.pages) <= 1:
            return reply
        now = utcnow()
        self._pages = {
            key: v for key, v in self._pages.items() if now - v[1] < PAGES_TTL
        }
        while len(self._pages) >= PAGES_KEPT:
            self._pages.pop(next(iter(self._pages)))
        token = secrets.token_hex(4)
        self._pages[token] = (reply.pages, now)
        return self._page(token, 0)

    def _page(self, token: str, index: int) -> Reply:
        stored = self._pages.get(token)
        if stored is None:
            return Reply(t("router.pages_expired"), edit=True)
        pages = stored[0]
        index = max(0, min(index, len(pages) - 1))
        buttons = []
        if index > 0:
            buttons.append(Button(t("router.button.previous"), f"pg:{token}:{index - 1}"))
        if index < len(pages) - 1:
            buttons.append(Button(t("router.button.next"), f"pg:{token}:{index + 1}"))
        text = f"{pages[index]}\n\n" + t("router.page_footer", page=index + 1, total=len(pages))
        return Reply(text=text, buttons=buttons, edit=True)

    async def handle_callback(self, data: str, user_id: int | None = None) -> Reply:
        """Answer to an inline button ("confirm:<token>", "deny:<token>", "pg:<token>:<n>")."""
        verb, _, token = data.partition(":")
        if verb == "pg":
            token, _, index = token.partition(":")
            return self._page(token, int(index) if index.isdigit() else 0)
        if verb == "stop":
            from uva.commands.session import stop_callback

            return await self._safely(stop_callback(self.services, data))
        if verb in ("rcvok", "rcvno"):  # "I was shut down while answering ... Do it again?"
            return await self._recover(verb, token, user_id)
        if verb in ("rmdok", "rmd1h", "rmdtm"):  # under a reminder's notice
            from uva.reminders import handle_button

            await emit(EventType.MESSAGE_RECEIVED, text=f"[button] {verb}", kind="callback")
            reply = await self._safely(self._text_reply(handle_button(self.services, verb, token)))
            await self._remember(f"[button] {verb}", reply)
            return reply
        if verb in ("wrok", "wrno"):
            await emit(EventType.MESSAGE_RECEIVED, text=f"[button] {verb}", kind="callback")
            reply = await self._safely(self._resolve_write(verb, token))
            await self._remember(f"[button] {verb}", reply)
            return reply
        if verb in ("shok", "shno", "shal") and self.services.shell is not None:
            await emit(EventType.MESSAGE_RECEIVED, text=f"[button] {verb}", kind="callback")
            reply = await self._safely(self.services.shell.resolve(verb, token))
            await self._remember(f"[button] {verb}", reply)
            return self._first_page(reply)
        pending = self._pending.pop(token, None)
        with trace(pending.trace_id if pending else None):
            await emit(EventType.MESSAGE_RECEIVED, text=f"[button] {verb}", kind="callback")
            return await self._safely(self._resolve_confirmation(verb, pending, user_id))

    async def _recover(self, verb: str, token: str, user_id: int | None) -> Reply:
        from uva.journal import TEXT_KINDS, journal_for

        journal = journal_for(self.services)
        entry = await journal.get(int(token)) if journal is not None and token.isdigit() else None
        if verb == "rcvno" or entry is None or entry.kind not in TEXT_KINDS:
            return Reply(t("main.recover.dropped"))
        return await self.handle_text(entry.text, user_id=user_id)

    @staticmethod
    async def _text_reply(coro) -> Reply:
        return Reply(await coro)

    async def _resolve_write(self, verb: str, token: str) -> Reply:
        """A write the injection scanner held back: run it now, or drop it."""
        from uva.llm.agent import execute_action
        from uva.actions.registry import ActionContext

        held = self.services.held_writes.pop(token)
        if held is None:
            return Reply(t("router.write.expired"))
        if verb != "wrok":
            await emit(EventType.ACTION, name=held.action, held=True, ok=False, denied=True)
            return Reply(t("mcp.denied") if held.kind == "mcp" else t("router.write.denied"))
        action, params = self.services.actions.validate(held.action, held.params)
        ctx = ActionContext(self.services, run_command=self._run_command_for_llm, confirmed=True,
                            tainted=held.tainted)
        result = await execute_action(action, params, ctx)
        return Reply(result.text or self.services.say("confirmacao"), buttons=result.buttons)

    async def _count(self, name: str) -> None:
        """Command usage for the curator; idle plugins are recomputed hourly."""
        services = self.services
        if services.sqlite is None:
            return
        from uva.curator import REFRESH_EVERY, record_command, refresh_idle

        await record_command(services.sqlite, name)
        now = utcnow()
        if self._curated_at is None or now - self._curated_at >= REFRESH_EVERY:
            self._curated_at = now
            try:
                await refresh_idle(services, now)
            except Exception as exc:
                await emit(EventType.ERROR, where="curator", error=repr(exc))

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
            reply = Reply(t("router.error_logged", phrase=self.services.say("erro_generico")), failed=True)
        await emit(EventType.REPLY, length=len(reply.text), file=reply.document is not None)
        return reply

    async def _route(
        self, text: str, user_id: int | None, photo: str | None = None, skill: str | None = None,
        note: str = "", images: tuple[str, ...] = (),
    ) -> Reply:
        # Teaching a sticker: its meaning, or "toda vez que eu mandar essa figurinha...".
        if photo is None and (reply := await self._try_sticker_text(text)) is not None:
            return reply
        # Tier 1: explicit command (captions too: "/decidir ..." uses the photo).
        if text.startswith("/"):
            return await self._dispatch_command(text, user_id)

        # A skill named by the caller (a routine or goal that has one).
        if skill and self.services.skills is not None and (found := self.services.skills.get(skill)):
            return await self.agent.handle(text, image=photo, images=images, skill=found, note=note)
        # A message with a picture goes straight to the (vision) LLM.
        if photo is not None:
            return await self.agent.handle(text, image=photo, images=images, note=note)

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

        # Tier 3: the LLM (within budget), with the skill whose name or trigger
        # phrase is in the message, if any.
        matched = self.services.skills.match(text) if self.services.skills is not None else None
        return await self.agent.handle(text, skill=matched, note=note)

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
            text = self.services.say("comando_desconhecido", comando=name)
            return Reply(text)

        await emit(EventType.ROUTE, tier="command", command=command.name, invoked_as=name)
        raw = text.strip()[len(head):].strip()
        return await self.run_command(command.name, args, user_id, raw_args=raw)

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
            text=t("router.did_you_mean", suggestion=suggestion),
            buttons=[
                Button(t("router.button.yes"), f"confirm:{token}"),
                Button(t("router.button.no"), f"deny:{token}"),
            ],
        )

    async def _resolve_confirmation(
        self, verb: str, pending: PendingCommand | None, user_id: int | None
    ) -> Reply:
        if pending is None or pending.expires_at < utcnow():
            await emit(EventType.ROUTE, tier="confirm_expired")
            return Reply(t("router.confirm_expired"))
        if verb != "confirm":
            await emit(EventType.ROUTE, tier="confirm_denied", command=pending.name)
            return Reply(self.services.say("cancelado"))
        await emit(EventType.ROUTE, tier="confirmed", command=pending.name)
        return await self.run_command(pending.name, pending.args, user_id)

    def _expire_pending(self) -> None:
        now = utcnow()
        for token in [key for key, p in self._pending.items() if p.expires_at < now]:
            del self._pending[token]

    async def _run_command_for_llm(self, name: str, args: list[str]) -> Reply:
        return await self.run_command(name, args, self.services.settings.allowed_telegram_user_id)

    async def run_command(
        self, name: str, args: list[str], user_id: int | None, raw_args: str | None = None
    ) -> Reply:
        command = self.registry.get(name)
        if command is None:
            raise KeyError(name)
        await emit(EventType.COMMAND, name=command.name, args=args)
        raw = " ".join(args) if raw_args is None else raw_args
        ctx = CommandContext(
            args=args, services=self.services, user_id=user_id, raw_args=raw, ask=self.agent.handle,
            reroute=lambda text: self._route(text, user_id),
        )
        await self._count(command.name)
        reply = Reply.coerce(await command.handler(ctx))
        if reply.summary is None:
            first_line = reply.text.strip().splitlines()[0] if reply.text.strip() else ""
            invoked = " ".join([f"/{command.name}", *args])
            reply.summary = f"{invoked} → {first_line[:150]}"
        return reply

