"""Where content came from, written by code and never parsed from text.

Origins: ``owner`` (the user typed or said it), ``agent`` (UVA wrote it while
talking to him), ``external`` (forwards, documents, web pages, search results,
MCP tools) and ``system`` (proactive runs: routines, goals, initiatives).

* A turn that read outside content is *tainted*: writes into what the LLM reads
  again later (memory, profile, skills, ``data/``) then always ask the user
  first (``uva.llm.agent.execute_action``); a write he confirms is stored with
  origin ``external``.
* Proactive runs (``system``) never write facts about the user.
* The background review never reads ``external`` or ``system`` turns for facts.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

OWNER, AGENT, EXTERNAL, SYSTEM = "owner", "agent", "external", "system"
ORIGINS = (OWNER, AGENT, EXTERNAL, SYSTEM)

# Notes whose text came from outside: research results and saved documents/attachments.
EXTERNAL_NOTE_PREFIXES = ("03-recursos/Pesquisa - ", "03-recursos/pesquisa_", "05-anexos/")
# Actions whose output is outside content.
TAINTING_ACTIONS = frozenset({"search", "web_fetch"})
# Facts about the user: never written by proactive runs.
MEMORY_WRITES = frozenset({"memory_save", "memory_update"})

# Who is talking in this turn: the user (owner) or a proactive run (system).
_turn_origin: ContextVar[str] = ContextVar("uva_turn_origin", default=OWNER)
# Outside content that arrived with the message (a forward, a document).
_incoming: ContextVar[tuple[str, ...]] = ContextVar("uva_incoming", default=())


def turn_origin() -> str:
    return _turn_origin.get()


@contextmanager
def system_turn() -> Iterator[None]:
    """A proactive run (routine, goal): its turns are ``system``."""
    token = _turn_origin.set(SYSTEM)
    try:
        yield
    finally:
        _turn_origin.reset(token)


@contextmanager
def incoming(*sources: str) -> Iterator[None]:
    """Outside content that came with this message ("forward", "document")."""
    token = _incoming.set(tuple(s for s in sources if s))
    try:
        yield
    finally:
        _incoming.reset(token)


def incoming_sources() -> tuple[str, ...]:
    return _incoming.get()


def external_note(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in EXTERNAL_NOTE_PREFIXES)


def taint_of(name: str, params: dict, data: dict) -> str | None:
    """The taint source an action's result brings into the turn, or None."""
    if name in TAINTING_ACTIONS or name.startswith("mcp_"):
        return name
    if name == "note_read" and external_note(str(data.get("path") or params.get("note") or "")):
        return f"note:{data.get('path') or params.get('note')}"
    if name == "document_read":
        return "document"
    if name == "recall":
        notes = [n for n in data.get("notes", []) if external_note(n)]
        return f"note:{notes[0]}" if notes else None
    return None


def turn_role_origin(role: str) -> str:
    """Origin of a conversation-log turn written now."""
    if role == "notice" or turn_origin() == SYSTEM:
        return SYSTEM
    return AGENT if role == "bot" else OWNER
