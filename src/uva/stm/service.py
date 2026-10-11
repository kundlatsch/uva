"""ShortTermMemory: the API the bot and the worker use for the session."""

from __future__ import annotations

from datetime import datetime, timedelta

from uva.clock import format_local, utcnow
from uva.config import Settings
from uva.llm.base import LLMMessage
from uva.stm.session import NOTICE_CHARS, TURN_CHARS, Ref, RefKind, RefStatus, Session, SessionBackend, Turn

ICONS = {"na fila": "⏳", "rodando": "🔄", "ok": "✅", "falhou": "❌"}


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
        def change(session: Session) -> None:
            session.turns.append(Turn(role="user", text=_clip(user_text)))
            session.turns.append(Turn(role="bot", text=_clip(bot_text)))

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
                prefix = "[aviso que eu mandei] " if turn.role == "notice" else ""
                messages.append(LLMMessage("assistant", prefix + turn.text))
        return messages

    def context_block(self, session: Session, tz, now: datetime | None = None) -> str:
        """The "Contexto recente" block for the system prompt ('' if empty)."""
        if not session.refs and not session.turns:
            return ""
        lines = []
        if session.refs:
            lines.append("Coisas que você fez/disparou nesta sessão (mais recente por último):")
            for ref in session.refs:
                lines.append(describe_ref(ref, tz, focus=ref.ref == session.focus))
        notices = [t for t in session.turns if t.role == "notice"][-3:]
        if notices:
            lines.append("Últimos avisos que você mandou sozinha:")
            lines += [f"- {format_local(t.at, tz, '%H:%M')}: {t.text[:200]}" for t in notices]
        return "\n".join(lines)


def describe_ref(ref: Ref, tz, focus: bool = False) -> str:
    when = format_local(ref.quando, tz, "%H:%M")
    error = f" — erro: {ref.erro}" if ref.erro else ""
    mark = " ← em foco" if focus else ""
    return f"- {ref.ref} [{ref.tipo}] {ref.rotulo} (id {ref.id}, {when}): {ICONS[ref.status]} {ref.status}{error}{mark}"


def _clip(text: str, limit: int = TURN_CHARS) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"
