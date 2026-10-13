"""/reverter: undo the last commit the bot made to its own repository."""

from __future__ import annotations

from uva.commands.registry import CommandContext, CommandRegistry
from uva.logging import get_logger
from uva.logging.events import EventType, emit
from uva.selfmod.features import FeatureStatus
from uva.selfmod.git import GitError

log = get_logger(__name__)


def register(registry: CommandRegistry) -> None:
    @registry.command(
        "reverter",
        "Desfaz a última mudança que eu fiz no meu repositório (/reverter brain: no second brain)",
        aliases=("desfazer",),
        examples=("desfaz o que voce mudou",),
    )
    async def revert(ctx: CommandContext) -> str:
        if ctx.args and ctx.args[0].lower() in ("brain", "notas"):
            return await _revert_brain(ctx.services)
        repo = ctx.services.repo
        if repo is None:
            return "Não tenho acesso ao repositório (git) aqui."
        commit = await repo.last_bot_commit()
        if commit is None:
            return "Não achei nenhuma mudança minha para desfazer."
        try:
            new_sha = await repo.revert(commit)
        except GitError as exc:
            await emit(EventType.SELFCODE, op="revert", sha=commit.sha, ok=False, error=str(exc))
            return f"Não consegui reverter {commit.sha[:7]} ({commit.subject}): {exc}"

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
        return f"↩️ Revertido {commit.sha[:7]}: {commit.subject} (novo commit {new_sha[:7]})."


async def _revert_brain(services) -> str:
    repo = services.brain_repo
    if repo is None:
        return "O second brain não tem histórico (git) aqui."
    try:
        commit = await repo.last_bot_commit()
    except GitError:
        commit = None
    if commit is None:
        return "Não achei nenhuma mudança minha no second brain para desfazer."
    try:
        new_sha = await repo.revert(commit)
    except GitError as exc:
        return f"Não consegui reverter {commit.sha[:7]} ({commit.subject}): {exc}"
    await emit(EventType.SELF_EDIT, op="revert_brain", sha=commit.sha, revert_sha=new_sha)
    return f"↩️ Second brain: desfeito {commit.sha[:7]} ({commit.subject})."
