"""Short-term memory: what was said and done in the current session.

One session per user, stored as a JSON document in Redis (``stm:<user_id>``)
and shared by the bot and the worker. See docs/memoria-curto-prazo.md.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from uva.clock import utcnow

RefKind = Literal["tarefa", "pesquisa", "nota", "objetivo", "feature", "gasto"]
RefStatus = Literal["na fila", "rodando", "ok", "falhou"]
ACTIVE: tuple[RefStatus, ...] = ("na fila", "rodando")
TURN_CHARS = 500


class Turn(BaseModel):
    role: Literal["user", "bot", "notice"]  # notice = proactive message the bot sent
    text: str
    at: datetime = Field(default_factory=utcnow)


class Ref(BaseModel):
    ref: str  # short handle, e.g. "t3"
    tipo: RefKind
    id: str  # task/feature/goal id, note path, expense id
    rotulo: str  # human label ("pesquisa: Fórmula 1")
    status: RefStatus = "ok"
    erro: str | None = None
    quando: datetime = Field(default_factory=utcnow)
    updated: datetime = Field(default_factory=utcnow)

    @property
    def active(self) -> bool:
        return self.status in ACTIVE


class Session(BaseModel):
    turns: list[Turn] = Field(default_factory=list)
    refs: list[Ref] = Field(default_factory=list)
    focus: str | None = None  # ref handle
    counter: int = 0
    last_activity: datetime = Field(default_factory=utcnow)

    @property
    def has_active_refs(self) -> bool:
        return any(r.active for r in self.refs)

    def focused(self) -> Ref | None:
        return next((r for r in self.refs if r.ref == self.focus), None) or (
            self.refs[-1] if self.refs else None
        )

    def find(self, query: str | None = None, tipo: str | None = None) -> Ref | None:
        """Resolve "t3", an id, a word from the label, a kind, or the focus."""
        if query:
            q = query.strip().lower()
            for ref in reversed(self.refs):
                if q in (ref.ref, ref.id.lower()):
                    return ref
            for ref in reversed(self.refs):
                if q in ref.rotulo.lower():
                    return ref
            return None
        if tipo:
            focused = self.focused()
            if focused is not None and focused.tipo == tipo:
                return focused
            return next((r for r in reversed(self.refs) if r.tipo == tipo), None)
        return self.focused()

    def trim(self, now: datetime, ttl: timedelta, max_turns: int, max_refs: int) -> None:
        """Drop old turns/refs; refs still running are kept until they finish."""
        cutoff = now - ttl
        self.turns = [t for t in self.turns if t.at >= cutoff][-max_turns:]
        self.refs = [r for r in self.refs if r.active or r.updated >= cutoff]
        while len(self.refs) > max_refs:
            finished = next((r for r in self.refs if not r.active), None)
            self.refs.remove(finished or self.refs[0])
        if self.focus and not any(r.ref == self.focus for r in self.refs):
            self.focus = self.refs[-1].ref if self.refs else None


class SessionBackend(Protocol):
    async def load(self, user_id: int) -> Session | None: ...

    async def update(
        self, user_id: int, change: Callable[[Session], None], ttl: timedelta
    ) -> Session:
        """Apply ``change`` atomically and save (TTL unless refs are running)."""
        ...

    async def delete(self, user_id: int) -> None: ...

    async def users(self) -> list[int]: ...
