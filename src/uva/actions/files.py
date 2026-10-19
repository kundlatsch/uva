"""file_read / file_write / file_append: the bot editing its own allowed files."""

from __future__ import annotations

from pydantic import BaseModel, Field

from uva.actions.registry import ActionContext, ActionRegistry, ActionResult
from uva.i18n import t
from uva.logging.events import EventType, emit
from uva.selfmod.files import FileAccessError

READ_LIMIT = 4000
PATH_HELP = (
    "Relative path: data/soul.md, data/fallback_phrases.json, data/<file>.md "
    "or brain/<folder>/<note>.md"
)


class FileReadParams(BaseModel):
    path: str = Field(description=PATH_HELP)


class FileWriteParams(BaseModel):
    path: str = Field(description=PATH_HELP)
    content: str = Field(description="Full content of the file")
    reason: str = Field(default="", description="Why the change (goes into the commit message)")


class FileAppendParams(BaseModel):
    path: str = Field(description=PATH_HELP)
    content: str = Field(description="Text to append at the end")
    reason: str = Field(default="", description="Why the change")


class ProfilePatchParams(BaseModel):
    old: str = Field(default="", description="Exact text of data/profile.md to replace; empty = append")
    new: str = Field(description="Replacement text (or the text to append), in Portuguese")
    reason: str = Field(default="", description="Why")


PROFILE = "data/profile.md"


def not_configured() -> ActionResult:
    return ActionResult(text=t("actions.files.not_configured"))


def register(registry: ActionRegistry) -> None:
    @registry.action(
        "file_read",
        "Reads one of my files (soul.md, data/ or the second brain)",
        FileReadParams,
    )
    async def file_read(params: FileReadParams, ctx: ActionContext) -> ActionResult:
        files = ctx.services.files
        if files is None:
            return not_configured()
        try:
            text = files.read(params.path)
        except (FileAccessError, FileNotFoundError) as exc:
            return ActionResult(text=t("actions.files.read_failed", path=params.path, error=exc), data={"ok": False})
        shown = text if len(text) <= READ_LIMIT else text[:READ_LIMIT] + "…"
        return ActionResult(text=f"{params.path}:\n\n{shown}", data={"ok": True, "chars": len(text)})

    @registry.action(
        "file_write",
        "Rewrites one of my files in data/ (e.g. soul.md) or creates a new note in the second brain",
        FileWriteParams,
    )
    async def file_write(params: FileWriteParams, ctx: ActionContext) -> ActionResult:
        return await _change(ctx, "write", params.path, params.content, params.reason)

    @registry.action(
        "file_append",
        "Appends text to the end of one of my files (data/ or the second brain)",
        FileAppendParams,
    )
    async def file_append(params: FileAppendParams, ctx: ActionContext) -> ActionResult:
        return await _change(ctx, "append", params.path, params.content, params.reason)

    @registry.action(
        "profile_patch",
        "Changes part of the user's profile (data/profile.md) in place, or appends to it, within its size limit",
        ProfilePatchParams,
    )
    async def profile_patch(params: ProfilePatchParams, ctx: ActionContext) -> ActionResult:
        files = ctx.services.files
        if files is None:
            return not_configured()
        try:
            current = files.read(PROFILE)
        except FileNotFoundError:
            current = ""
        except FileAccessError as exc:
            return ActionResult(text=t("actions.files.read_failed", path=PROFILE, error=exc), data={"ok": False})
        if params.old:
            count = current.count(params.old)
            if count != 1:
                return ActionResult(text=t("actions.files.patch_not_unique", path=PROFILE, count=count), data={"ok": False})
            content = current.replace(params.old, params.new)
        else:
            content = current.rstrip("\n") + ("\n" if current.strip() else "") + params.new.strip() + "\n"
        return await _change(ctx, "write", PROFILE, content, params.reason or "profile_patch")


async def _change(ctx: ActionContext, op: str, path: str, content: str, reason: str) -> ActionResult:
    files = ctx.services.files
    if files is None:
        return not_configured()
    try:
        change = await (files.write if op == "write" else files.append)(path, content, reason)
    except (FileAccessError, OSError) as exc:
        await emit(EventType.SELF_EDIT, op=op, path=path, ok=False, error=str(exc))
        return ActionResult(text=t("actions.files.change_failed", path=path, error=exc), data={"ok": False})
    await emit(EventType.SELF_EDIT, op=op, path=change.target.display, ok=True, commit=change.commit)
    commit = t("actions.files.commit", commit=change.commit[:7]) if change.commit else ""
    return ActionResult(
        text=t("actions.files.updated", path=change.target.display, commit=commit),
        data={"ok": True, "commit": change.commit},
    )
