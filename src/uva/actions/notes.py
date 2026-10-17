"""Second-brain actions: create, append, search and read Markdown notes."""

from __future__ import annotations

import asyncio
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from uva.brain.naming import portuguese_title, require_title_pt
from uva.actions.registry import ActionContext, ActionRegistry, ActionResult
from uva.brain.vault import NoteNotFoundError, Vault
from uva.i18n import t
from uva.stm.refs import remember_ref

Folder = Literal["inbox", "projetos", "areas", "recursos", "arquivo"]
READ_LIMIT = 3000


class NoteCreateParams(BaseModel):
    model_config = ConfigDict(json_schema_extra=require_title_pt)

    title_pt: str = Field(
        default="",
        description=(
            "REQUIRED. Title in Brazilian Portuguese (it is the user's content): becomes the file "
            "name (snake_case, no accents) and the note's title. Never English."
        ),
    )
    title: str = Field(default="", description="(deprecated: use title_pt)")
    body: str = Field(description="Content in Markdown")
    folder: Folder = Field(
        default="inbox",
        description="inbox (incoming), projetos, areas, recursos (reference/research) or arquivo",
    )
    subfolder: str = Field(
        default="", description="Subfolder inside the chosen folder (e.g. Saúde or Saúde/Exercícios)"
    )
    tags: list[str] = Field(default_factory=list)
    links: list[str] = Field(default_factory=list, description="Titles of related notes")


class FolderCreateParams(BaseModel):
    name: str = Field(min_length=1, description="Name of the new folder (may have sublevels: A/B)")
    folder: Folder = Field(
        description=(
            "Where to create it, by PARA: projetos (goal with a deadline), areas (ongoing "
            "responsibility: health, finances, home), recursos (topics of interest/reference), "
            "arquivo (inactive), inbox (no clear place)"
        )
    )


class NoteAppendParams(BaseModel):
    note: str = Field(description="Title or path of the existing note")
    text: str = Field(description="Text to append at the end")


class NoteUpdateParams(BaseModel):
    note: str = Field(description="Title or path of the existing note")
    title_pt: str | None = Field(
        default=None, description="New title in Brazilian Portuguese (renames the file); omit to keep it"
    )
    title: str | None = Field(default=None, description="(deprecated: use title_pt)")
    body: str | None = Field(
        default=None,
        description="New FULL content (without the YAML header). Omit it to only rename",
    )
    tags: list[str] | None = None


class NoteSearchParams(BaseModel):
    term: str = Field(description="Term to look for in titles and contents (the notes are in Portuguese)")


class NoteReadParams(BaseModel):
    note: str = Field(description="Title or path of the note")


class RecallParams(BaseModel):
    query: str = Field(description="What to look for (keywords; the user's content is in Portuguese)")


def _vault(ctx: ActionContext) -> Vault | None:
    return ctx.services.vault


def no_vault() -> ActionResult:
    return ActionResult(text=t("actions.notes.no_vault"))


def register(registry: ActionRegistry) -> None:
    @registry.action(
        "note_create", "Creates a note in the second brain", NoteCreateParams, proactive_safe=True
    )
    async def note_create(params: NoteCreateParams, ctx: ActionContext) -> ActionResult:
        if (vault := _vault(ctx)) is None:
            return no_vault()
        title = await portuguese_title(ctx.services, params.title_pt or params.title, params.body, scope=ctx.scope)
        try:
            note = vault.create_note(
                title, params.body, folder=params.folder, subfolder=params.subfolder,
                tags=params.tags, links=params.links,
            )
        except ValueError as exc:  # bad folder or placeholders instead of content
            return ActionResult(text=t("actions.notes.create_failed", error=exc), data={"ok": False})
        vault.append_daily(f"Nota criada: {note.link}")
        await remember_ref(ctx.services, "nota", note.path, f"note: {note.title}")
        return ActionResult(text=t("actions.notes.created", path=note.path), data={"path": note.path})

    @registry.action(
        "folder_create",
        "Creates a folder in the second brain (you pick the place by PARA) with an empty note",
        FolderCreateParams,
    )
    async def folder_create(params: FolderCreateParams, ctx: ActionContext) -> ActionResult:
        if (vault := _vault(ctx)) is None:
            return no_vault()
        try:
            folder, placeholder = vault.create_folder(params.folder, params.name)
        except ValueError as exc:
            return ActionResult(text=t("actions.notes.folder_failed", error=exc), data={"ok": False})
        if placeholder is None:
            return ActionResult(text=t("actions.notes.folder_exists", folder=folder), data={"folder": folder})
        vault.append_daily(f"Pasta criada: {folder}")
        await remember_ref(ctx.services, "nota", placeholder, f"folder: {folder}")
        return ActionResult(
            text=t("actions.notes.folder_created", folder=folder, placeholder=placeholder),
            data={"folder": folder, "placeholder": placeholder},
        )

    @registry.action(
        "note_append", "Appends text to an existing note", NoteAppendParams, proactive_safe=True
    )
    async def note_append(params: NoteAppendParams, ctx: ActionContext) -> ActionResult:
        if (vault := _vault(ctx)) is None:
            return no_vault()
        try:
            note = vault.append(params.note, params.text)
        except NoteNotFoundError:
            return ActionResult(text=t("actions.notes.not_found", note=params.note), data={"found": False})
        except ValueError as exc:
            return ActionResult(text=t("actions.notes.not_changed", error=exc), data={"ok": False})
        return ActionResult(text=t("actions.notes.appended", path=note.path), data={"path": note.path})

    @registry.action(
        "note_update",
        "Fixes an existing note: renames it and/or rewrites its whole content (the previous "
        "version is kept in 04-arquivo/Versões)",
        NoteUpdateParams,
    )
    async def note_update(params: NoteUpdateParams, ctx: ActionContext) -> ActionResult:
        if (vault := _vault(ctx)) is None:
            return no_vault()
        try:
            new_title = params.title_pt or params.title
            if new_title:
                new_title = await portuguese_title(ctx.services, new_title, params.body or "", scope=ctx.scope)
            note, backup = vault.update_note(params.note, title=new_title, body=params.body, tags=params.tags)
        except NoteNotFoundError:
            return ActionResult(text=t("actions.notes.not_found", note=params.note), data={"found": False})
        except ValueError as exc:  # placeholders instead of content
            return ActionResult(text=t("actions.notes.not_changed", error=exc), data={"ok": False})
        vault.append_daily(f"Nota atualizada: {note.link}")
        await remember_ref(ctx.services, "nota", note.path, f"note: {note.title}")
        return ActionResult(
            text=t("actions.notes.updated", path=note.path, backup=backup),
            data={"path": note.path, "backup": backup},
        )

    @registry.action(
        "note_search", "Searches notes in the second brain", NoteSearchParams, proactive_safe=True,
        lookup=True,
    )
    async def note_search(params: NoteSearchParams, ctx: ActionContext) -> ActionResult:
        if (vault := _vault(ctx)) is None:
            return no_vault()
        hits = await asyncio.to_thread(vault.search, params.term)
        if not hits:
            text = ctx.services.say("sem_resultado", termo=params.term)
            return ActionResult(
                text=t("actions.notes.no_hits", phrase=text, term=params.term), data={"hits": []}
            )
        lines = [t("actions.notes.hits", term=params.term)] + [f"- {h.path}: {h.snippet}" for h in hits]
        return ActionResult(
            text="\n".join(lines), data={"hits": [{"path": h.path, "title": h.title} for h in hits]}
        )

    @registry.action(
        "note_read", "Reads a note from the second brain", NoteReadParams, proactive_safe=True, lookup=True
    )
    async def note_read(params: NoteReadParams, ctx: ActionContext) -> ActionResult:
        if (vault := _vault(ctx)) is None:
            return no_vault()
        try:
            note = vault.read(params.note)
        except NoteNotFoundError:
            return ActionResult(text=t("actions.notes.not_found", note=params.note), data={"found": False})
        body = note.body if len(note.body) <= READ_LIMIT else note.body[:READ_LIMIT] + "…"
        return ActionResult(text=f"{note.path}\n\n{body}", data={"path": note.path, "body": body})

    @registry.action(
        "recall",
        "Searches ALL your systems at once (long-term memory, brain notes, goals, "
        "expenses) by keywords",
        RecallParams,
        proactive_safe=True,
        lookup=True,
    )
    async def recall_action(params: RecallParams, ctx: ActionContext) -> ActionResult:
        from uva.recall import recall

        found = await recall(ctx.services, params.query)
        if not found:
            return ActionResult(text=t("actions.notes.recall_none", query=params.query), data={"found": False})
        return ActionResult(text=found.render(), data={"notes": found.notes})
