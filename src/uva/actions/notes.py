"""Second-brain actions: create, append, search and read Markdown notes."""

from __future__ import annotations

import asyncio
from typing import Literal

from pydantic import BaseModel, Field

from uva.actions.registry import ActionContext, ActionRegistry, ActionResult
from uva.brain.vault import NoteNotFoundError, Vault

Folder = Literal["inbox", "projetos", "areas", "recursos", "arquivo"]
READ_LIMIT = 3000


class NoteCreateParams(BaseModel):
    title: str = Field(description="Título curto e claro (vira o nome do arquivo)")
    body: str = Field(description="Conteúdo em Markdown")
    folder: Folder = Field(
        default="inbox",
        description="inbox (entrada), projetos, areas, recursos (referência/pesquisas) ou arquivo",
    )
    tags: list[str] = Field(default_factory=list)
    links: list[str] = Field(default_factory=list, description="Títulos de notas relacionadas")


class NoteAppendParams(BaseModel):
    note: str = Field(description="Título ou caminho da nota existente")
    text: str = Field(description="Texto a acrescentar no fim")


class NoteSearchParams(BaseModel):
    term: str = Field(description="Termo a procurar no título e no conteúdo")


class NoteReadParams(BaseModel):
    note: str = Field(description="Título ou caminho da nota")


def _vault(ctx: ActionContext) -> Vault | None:
    return ctx.services.vault


NO_VAULT = ActionResult(text="O second brain não está configurado.")


def register(registry: ActionRegistry) -> None:
    @registry.action(
        "note_create", "Cria uma nota no second brain", NoteCreateParams, proactive_safe=True
    )
    async def note_create(params: NoteCreateParams, ctx: ActionContext) -> ActionResult:
        if (vault := _vault(ctx)) is None:
            return NO_VAULT
        note = vault.create_note(
            params.title, params.body, folder=params.folder, tags=params.tags, links=params.links
        )
        vault.append_daily(f"Nota criada: [[{note.title}]]")
        return ActionResult(text=f"📝 Nota salva: {note.path}", data={"path": note.path})

    @registry.action(
        "note_append", "Acrescenta texto a uma nota existente", NoteAppendParams, proactive_safe=True
    )
    async def note_append(params: NoteAppendParams, ctx: ActionContext) -> ActionResult:
        if (vault := _vault(ctx)) is None:
            return NO_VAULT
        try:
            note = vault.append(params.note, params.text)
        except NoteNotFoundError:
            return ActionResult(text=f"Não achei a nota '{params.note}'.", data={"found": False})
        return ActionResult(text=f"📝 Texto acrescentado em {note.path}", data={"path": note.path})

    @registry.action(
        "note_search", "Procura notas no second brain", NoteSearchParams, proactive_safe=True
    )
    async def note_search(params: NoteSearchParams, ctx: ActionContext) -> ActionResult:
        if (vault := _vault(ctx)) is None:
            return NO_VAULT
        hits = await asyncio.to_thread(vault.search, params.term)
        if not hits:
            text = ctx.services.say("sem_resultado", termo=params.term)
            return ActionResult(text=f"{text}\n(nenhuma nota com '{params.term}')", data={"hits": []})
        lines = [f"Notas com '{params.term}':"] + [f"- {h.path}: {h.snippet}" for h in hits]
        return ActionResult(
            text="\n".join(lines), data={"hits": [{"path": h.path, "title": h.title} for h in hits]}
        )

    @registry.action("note_read", "Lê uma nota do second brain", NoteReadParams, proactive_safe=True)
    async def note_read(params: NoteReadParams, ctx: ActionContext) -> ActionResult:
        if (vault := _vault(ctx)) is None:
            return NO_VAULT
        try:
            note = vault.read(params.note)
        except NoteNotFoundError:
            return ActionResult(text=f"Não achei a nota '{params.note}'.", data={"found": False})
        body = note.body if len(note.body) <= READ_LIMIT else note.body[:READ_LIMIT] + "…"
        return ActionResult(text=f"{note.path}\n\n{body}", data={"path": note.path, "body": body})
