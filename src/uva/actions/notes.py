"""Second-brain actions: create, append, search and read Markdown notes."""

from __future__ import annotations

import asyncio
from typing import Literal

from pydantic import BaseModel, Field

from uva.actions.registry import ActionContext, ActionRegistry, ActionResult
from uva.brain.vault import NoteNotFoundError, Vault
from uva.stm.refs import remember_ref

Folder = Literal["inbox", "projetos", "areas", "recursos", "arquivo"]
READ_LIMIT = 3000


class NoteCreateParams(BaseModel):
    title: str = Field(description="Título curto e claro (vira o nome do arquivo)")
    body: str = Field(description="Conteúdo em Markdown")
    folder: Folder = Field(
        default="inbox",
        description="inbox (entrada), projetos, areas, recursos (referência/pesquisas) ou arquivo",
    )
    subfolder: str = Field(
        default="", description="Subpasta dentro da pasta escolhida (ex.: Saúde ou Saúde/Exercícios)"
    )
    tags: list[str] = Field(default_factory=list)
    links: list[str] = Field(default_factory=list, description="Títulos de notas relacionadas")


class FolderCreateParams(BaseModel):
    name: str = Field(min_length=1, description="Nome da pasta nova (pode ter subníveis: A/B)")
    folder: Folder = Field(
        description=(
            "Onde criar, pelo PARA: projetos (meta com prazo), areas (responsabilidade contínua: "
            "saúde, finanças, casa), recursos (assuntos de interesse/referência), arquivo "
            "(inativo), inbox (sem lugar claro)"
        )
    )


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
        try:
            note = vault.create_note(
                params.title, params.body, folder=params.folder, subfolder=params.subfolder,
                tags=params.tags, links=params.links,
            )
        except ValueError as exc:
            return ActionResult(text=f"Não criei a nota: {exc}", data={"ok": False})
        vault.append_daily(f"Nota criada: [[{note.title}]]")
        await remember_ref(ctx.services, "nota", note.path, f"nota: {note.title}")
        return ActionResult(text=f"📝 Nota salva: {note.path}", data={"path": note.path})

    @registry.action(
        "folder_create",
        "Cria uma pasta no second brain (você escolhe o lugar pelo PARA) com uma nota vazia",
        FolderCreateParams,
    )
    async def folder_create(params: FolderCreateParams, ctx: ActionContext) -> ActionResult:
        if (vault := _vault(ctx)) is None:
            return NO_VAULT
        try:
            folder, placeholder = vault.create_folder(params.folder, params.name)
        except ValueError as exc:
            return ActionResult(text=f"Não criei a pasta: {exc}", data={"ok": False})
        if placeholder is None:
            return ActionResult(text=f"📁 A pasta {folder} já existia (não mexi em nada).", data={"folder": folder})
        vault.append_daily(f"Pasta criada: {folder}")
        await remember_ref(ctx.services, "nota", placeholder, f"pasta: {folder}")
        return ActionResult(
            text=f"📁 Pasta criada: {folder} (com a nota vazia {placeholder})",
            data={"folder": folder, "placeholder": placeholder},
        )

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
