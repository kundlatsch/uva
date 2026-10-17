"""/reverter: undo the last commit the bot made to its own repository."""

from __future__ import annotations

from uva.commands.registry import CommandContext, CommandRegistry
from uva.i18n import t
from uva.logging import get_logger
from uva.logging.events import EventType, emit
from uva.selfmod.features import FeatureStatus
from uva.selfmod.git import GitError

log = get_logger(__name__)


def register(registry: CommandRegistry) -> None:
    @registry.command(
        "reverter",
        t("commands.revert.reverter.description"),
        aliases=("desfazer", "revert", "undo"),
        examples=("desfaz o que voce mudou",),
    )
    async def revert(ctx: CommandContext) -> str:
        if ctx.args and ctx.args[0].lower() in ("brain", "notas", "notes"):
            return await _revert_brain(ctx.services)
        repo = ctx.services.repo
        if repo is None:
            return t("commands.revert.reverter.no_repo")
        commit = await repo.last_bot_commit()
        if commit is None:
            return t("commands.revert.reverter.nothing")
        try:
            new_sha = await repo.revert(commit)
        except GitError as exc:
            await emit(EventType.SELFCODE, op="revert", sha=commit.sha, ok=False, error=str(exc))
            return t("commands.revert.reverter.failed", sha=commit.sha[:7], subject=commit.subject, error=exc)

        await emit(EventType.SELFCODE, op="revert", sha=commit.sha, revert_sha=new_sha, ok=True)
        feature_id = commit.trailer("UVA-Feature-Id")
        if feature_id and ctx.services.features is not None:
            await ctx.services.features.store.update(feature_id, status=FeatureStatus.REVERTED)
        if commit.kind == "self-code":
            for hook in ctx.services.on_code_changed:
                try:
                    await hook()
                except Exception as exc:  # reload problems are reported, not fatal
                    log.warning("code_changed_hook_failed", error=str(exc))
        return t(
            "commands.revert.reverter.done", sha=commit.sha[:7], subject=commit.subject, new_sha=new_sha[:7]
        )


async def _revert_brain(services) -> str:
    repo = services.brain_repo
    if repo is None:
        return t("commands.revert.reverter.brain_no_history")
    try:
        commit = await repo.last_bot_commit()
    except GitError:
        commit = None
    if commit is None:
        return t("commands.revert.reverter.brain_nothing")
    try:
        new_sha = await repo.revert(commit)
    except GitError as exc:
        return t("commands.revert.reverter.failed", sha=commit.sha[:7], subject=commit.subject, error=exc)
    await emit(EventType.SELF_EDIT, op="revert_brain", sha=commit.sha, revert_sha=new_sha)
    return t("commands.revert.reverter.brain_done", sha=commit.sha[:7], subject=commit.subject)
