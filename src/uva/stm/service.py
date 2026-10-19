"""ShortTermMemory: the API the bot and the worker use for the session."""

from __future__ import annotations

from datetime import datetime, timedelta

from uva.clock import format_local, utcnow
from uva.config import Settings
from uva.i18n import t
from uva.labels import voice_text
from uva.llm.base import LLMMessage
from uva.stm.session import NOTICE_CHARS, TURN_CHARS, VOICE_CHARS, Ref, RefKind, RefStatus, Session, SessionBackend, Turn

ICONS = {"na fila": "⏳", "rodando": "🔄", "ok": "✅", "falhou": "❌"}
# Stored status (kept in Portuguese in Redis) -> key of its label in stm.service.status.
STATUS_KEYS = {"na fila": "queued", "rodando": "running", "ok": "ok", "falhou": "failed"}


# What the LLM reads is always English; the user sees the configured language.
LLM_LANGUAGE = "en"


def status_label(status: str, lang: str | None = None) -> str:
    """How a stored status is shown ("rodando" -> "running" in English)."""
    key = STATUS_KEYS.get(status)
    return t(f"stm.service.status.{key}", lang=lang) if key else status


def kind_label(kind: str, lang: str | None = None) -> str:
    """How a stored reference kind is shown ("pesquisa" -> "research" in English)."""
    label = t(f"stm.service.kind.{kind}", lang=lang)
    return kind if label == f"stm.service.kind.{kind}" else label


class ShortTermMemory:
    def __init__(self, backend: SessionBackend, settings: Settings) -> None:
        self.backend = backend
        self.settings = settings
        self.owner = settings.allowed_telegram_user_id  # single-user bot

    @property
    def ttl(self) -> timedelta:
        return timedelta(hours=self.settings.stm_ttl_hours)

    async def _update(self, change, user_id: int | None = None) -> Session:
        s = self.settings

        def apply(session: Session) -> None:
            change(session)
            now = utcnow()
            session.last_activity = now
            session.trim(now, self.ttl, s.stm_max_turns, s.stm_max_refs)

        return await self.backend.update(user_id or self.owner, apply, self.ttl)

    # --- reading -------------------------------------------------------------------

    async def session(self, user_id: int | None = None) -> Session:
        session = await self.backend.load(user_id or self.owner) or Session()
        s = self.settings
        session.trim(utcnow(), self.ttl, s.stm_max_turns, s.stm_max_refs)
        return session

    async def clear(self, user_id: int | None = None) -> None:
        await self.backend.delete(user_id or self.owner)

    # --- conversation ----------------------------------------------------------------

    async def add_turn(self, role: str, text: str, user_id: int | None = None) -> None:
        turn = Turn(role=role, text=_clip(text, NOTICE_CHARS if role == "notice" else TURN_CHARS))
        await self._update(lambda s: s.turns.append(turn), user_id)

    async def add_exchange(self, user_text: str, bot_text: str, user_id: int | None = None) -> None:
        limit = VOICE_CHARS if voice_text(user_text) is not None else TURN_CHARS

        def change(session: Session) -> None:
            session.turns.append(Turn(role="user", text=_clip(user_text, limit)))
            session.turns.append(Turn(role="bot", text=_clip(bot_text)))

        await self._update(change, user_id)

    async def drop_last_exchange(self, user_id: int | None = None) -> int:
        """Remove the last user turn and the bot turns after it; returns how many."""
        dropped: list[int] = []

        def change(session: Session) -> None:
            index = next((i for i in range(len(session.turns) - 1, -1, -1) if session.turns[i].role == "user"), None)
            if index is None:
                return
            kept = [t for t in session.turns[index:] if t.role == "notice"]
            dropped.append(len(session.turns) - index - len(kept))
            session.turns = session.turns[:index] + kept

        await self._update(change, user_id)
        return dropped[0] if dropped else 0

    async def set_mode(self, mode: str | None, user_id: int | None = None) -> None:
        def change(session: Session) -> None:
            session.mode = mode

        await self._update(change, user_id)

    # --- references --------------------------------------------------------------------

    async def add_ref(
        self, tipo: RefKind, id: str, rotulo: str, status: RefStatus = "ok", user_id: int | None = None
    ) -> Ref:
        created: list[Ref] = []

        def change(session: Session) -> None:
            session.counter += 1
            ref = Ref(ref=f"t{session.counter}", tipo=tipo, id=id, rotulo=rotulo, status=status)
            session.refs.append(ref)
            session.focus = ref.ref
            created.append(ref)

        await self._update(change, user_id)
        return created[0]

    async def update_ref(
        self, id: str, status: RefStatus, erro: str | None = None, *, focus: bool = True,
        user_id: int | None = None,
    ) -> Ref | None:
        found: list[Ref] = []

        def change(session: Session) -> None:
            for ref in session.refs:
                if ref.id == id:
                    ref.status, ref.erro, ref.updated = status, erro, utcnow()
                    if focus:
                        session.focus = ref.ref
                    found.append(ref)

        await self._update(change, user_id)
        return found[-1] if found else None

    async def set_focus(self, handle: str, user_id: int | None = None) -> None:
        def change(session: Session) -> None:
            if any(r.ref == handle for r in session.refs):
                session.focus = handle

        await self._update(change, user_id)

    # --- for the LLM -----------------------------------------------------------------------

    def history(self, session: Session, limit: int) -> list[LLMMessage]:
        messages = []
        for turn in session.turns[-limit:]:
            if turn.role == "user":
                messages.append(LLMMessage("user", turn.text))
            else:
                prefix = "[notice I sent] " if turn.role == "notice" else ""
                messages.append(LLMMessage("assistant", prefix + turn.text))
        return messages

    def context_block(self, session: Session, tz, now: datetime | None = None) -> str:
        """The "Recent context" block for the system prompt ('' if empty), in English."""
        if not session.refs and not session.turns:
            return ""
        lines = []
        if session.refs:
            lines.append("Things you did/started in this session (most recent last):")
            for ref in session.refs:
                lines.append(describe_ref(ref, tz, focus=ref.ref == session.focus, lang=LLM_LANGUAGE))
        notices = [turn for turn in session.turns if turn.role == "notice"][-3:]
        if notices:
            lines.append("Latest notices you sent on your own:")
            lines += [f"- {format_local(turn.at, tz, '%H:%M')}: {turn.text[:200]}" for turn in notices]
        return "\n".join(lines)


def describe_ref(ref: Ref, tz, focus: bool = False, lang: str | None = None) -> str:
    """One line per reference; ``lang`` forces a language (English for the LLM)."""
    when = format_local(ref.quando, tz, "%H:%M")
    error = t("stm.service.error", lang=lang, error=ref.erro) if ref.erro else ""
    mark = t("stm.service.focus", lang=lang) if focus else ""
    return (
        f"- {ref.ref} [{kind_label(ref.tipo, lang)}] {ref.rotulo} (id {ref.id}, {when}): "
        f"{ICONS[ref.status]} {status_label(ref.status, lang)}{error}{mark}"
    )


def _clip(text: str, limit: int = TURN_CHARS) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"
