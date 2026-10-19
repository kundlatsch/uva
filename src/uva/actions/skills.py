"""skill_read / skill_create / skill_patch / skill_write_file: procedural memory.

Writes go through FileAccess (data/skills/...), so each change is a commit
with UVA-Self-Edit that /reverter can undo. A patch needs a fresh skill_read
of the same skill in this process (the LLM must see what it changes).
"""

from __future__ import annotations

from pathlib import PurePosixPath

from pydantic import BaseModel, Field

from uva.actions.registry import ActionContext, ActionRegistry, ActionResult
from uva.i18n import t
from uva.logging.events import EventType, emit
from uva.selfmod.files import FileAccessError
from uva.skills import Skill, SkillError, check_name, parse, render


class SkillReadParams(BaseModel):
    name: str = Field(description="Skill name, e.g. 'reading-notes'")
    file: str = Field(default="", description="A file under references/ (optional)")


class SkillCreateParams(BaseModel):
    name: str = Field(description="lowercase-with-dashes, 2-40 chars")
    description: str = Field(description="One line, max 120 chars: what kind of task it covers")
    body: str = Field(
        description=(
            "Markdown, in English, with the sections '## When to Use', '## Procedure', "
            "'## Pitfalls', '## Verification'"
        )
    )
    triggers: list[str] = Field(
        default_factory=list, description="Example phrases of the user that call for it (optional)"
    )


class SkillPatchParams(BaseModel):
    name: str
    old: str = Field(description="Exact text in SKILL.md to replace (read it first with skill_read)")
    new: str = Field(description="Replacement text")


class SkillWriteFileParams(BaseModel):
    name: str
    path: str = Field(description="File under references/, e.g. 'references/format.md'")
    content: str


def register(registry: ActionRegistry) -> None:
    @registry.action(
        "skill_read",
        "Reads one of your skills (how the user wants a kind of task done)",
        SkillReadParams,
        proactive_safe=True,
        lookup=True,
    )
    async def skill_read(params: SkillReadParams, ctx: ActionContext) -> ActionResult:
        # A lookup: the text goes back to the LLM (English).
        library = ctx.services.skills
        if library is None:
            return ActionResult(text="Skills are not available.")
        try:
            text = library.read(params.name, params.file)
        except SkillError as exc:
            return ActionResult(text=str(exc), data={"ok": False})
        if not params.file:
            await library.used(check_name(params.name))
        return ActionResult(text=text, data={"ok": True, "name": params.name})

    @registry.action(
        "skill_create",
        "Creates a skill: how the user wants a kind of task done (a lesson, not a log)",
        SkillCreateParams,
    )
    async def skill_create(params: SkillCreateParams, ctx: ActionContext) -> ActionResult:
        library = ctx.services.skills
        if library is None or ctx.services.files is None:
            return ActionResult(text=t("actions.skills.unavailable"), data={"ok": False})
        try:
            name = check_name(params.name)
            if library.get(name) is not None:
                raise SkillError(t("actions.skills.exists", name=name))
            skill = Skill(name, " ".join(params.description.split()), params.body.strip(),
                          tuple(params.triggers))
            text = render(skill)
            parse(text, name)  # same validation as loading
        except SkillError as exc:
            return ActionResult(text=t("actions.skills.invalid", error=exc), data={"ok": False})
        return await _write(ctx, name, skill.path, text, f"New skill: {skill.description}", "created")

    @registry.action(
        "skill_patch",
        "Fixes part of a skill in place (read it with skill_read first)",
        SkillPatchParams,
    )
    async def skill_patch(params: SkillPatchParams, ctx: ActionContext) -> ActionResult:
        library = ctx.services.skills
        if library is None or ctx.services.files is None:
            return ActionResult(text=t("actions.skills.unavailable"), data={"ok": False})
        skill = library.get(params.name)
        if skill is None:
            return ActionResult(text=t("actions.skills.missing", name=params.name), data={"ok": False})
        if not library.was_read(skill.name):
            return ActionResult(text=t("actions.skills.read_first", name=skill.name), data={"ok": False})
        current = render(skill)
        count = current.count(params.old) if params.old else 0
        if count != 1:
            key = "actions.skills.old_missing" if count == 0 else "actions.skills.old_ambiguous"
            return ActionResult(text=t(key, name=skill.name, count=count), data={"ok": False})
        try:
            patched = parse(current.replace(params.old, params.new), skill.name, skill.references)
            patched = Skill(patched.name, patched.description, patched.body, patched.triggers,
                            skill.version + 1, patched.references)
        except SkillError as exc:
            return ActionResult(text=t("actions.skills.invalid", error=exc), data={"ok": False})
        result = await _write(ctx, skill.name, skill.path, render(patched), f"Fix skill {skill.name}", "patched")
        if result.data.get("ok"):
            library.read(skill.name)  # it knows the new text: another patch may follow
        return result

    @registry.action(
        "skill_write_file",
        "Writes a reference file (references/*.md) of a skill: long details the skill points to",
        SkillWriteFileParams,
    )
    async def skill_write_file(params: SkillWriteFileParams, ctx: ActionContext) -> ActionResult:
        library = ctx.services.skills
        if library is None or ctx.services.files is None:
            return ActionResult(text=t("actions.skills.unavailable"), data={"ok": False})
        skill = library.get(params.name)
        if skill is None:
            return ActionResult(text=t("actions.skills.missing", name=params.name), data={"ok": False})
        rel = PurePosixPath(params.path.strip().replace("\\", "/"))
        if len(rel.parts) != 2 or rel.parts[0] != "references" or rel.suffix != ".md":
            return ActionResult(text=t("actions.skills.bad_reference", path=params.path), data={"ok": False})
        path = f"data/skills/{skill.name}/{rel.as_posix()}"
        return await _write(ctx, skill.name, path, params.content, f"Skill {skill.name}: {rel.name}", "file")


async def _write(ctx: ActionContext, name: str, path: str, content: str, reason: str, op: str) -> ActionResult:
    assert ctx.services.files is not None
    try:
        change = await ctx.services.files.write(path, content, reason)
    except (FileAccessError, OSError) as exc:
        await emit(EventType.SELF_EDIT, op=f"skill_{op}", path=path, ok=False, error=str(exc))
        return ActionResult(text=t("actions.skills.failed", name=name, error=exc), data={"ok": False})
    await emit(EventType.SELF_EDIT, op=f"skill_{op}", path=path, ok=True, commit=change.commit)
    commit = t("actions.files.commit", commit=change.commit[:7]) if change.commit else ""
    return ActionResult(
        text=t(f"actions.skills.{op}", name=name, path=path, commit=commit),
        data={"ok": True, "commit": change.commit, "name": name},
    )

