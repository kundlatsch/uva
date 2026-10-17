"""/figurinhas and /figurinha: what the bot learned about each sticker."""

from __future__ import annotations

from uva.commands.registry import CommandContext, CommandRegistry
from uva.i18n import t
from uva.replies import Reply
from uva.stickers import describe


def register(registry: CommandRegistry) -> None:
    @registry.command(
        "figurinhas",
        t("commands.stickers.figurinhas.description"),
        aliases=("stickers",),
        examples=("quais figurinhas voce conhece",),
    )
    async def stickers(ctx: CommandContext) -> Reply:
        repo = ctx.services.stickers
        if repo is None:
            return Reply(t("commands.stickers.not_configured"))
        items = await repo.all()
        if not items:
            return Reply(t("commands.stickers.figurinhas.none"))
        body = "\n".join(describe(s) for s in items)
        return Reply.paginated(
            f"{body}\n\n{t('commands.stickers.usage')}",
            header=t("commands.stickers.figurinhas.header", count=len(items)),
        )

    @registry.command(
        "figurinha",
        t("commands.stickers.figurinha.description"),
        aliases=("sticker",),
        usage="<n> significado|acao|sem-acao|remover [texto]",
    )
    async def sticker(ctx: CommandContext) -> Reply:
        repo = ctx.services.stickers
        if repo is None:
            return Reply(t("commands.stickers.not_configured"))
        usage = Reply(t("commands.stickers.usage"))
        args = ctx.args
        if len(args) < 2 or not args[0].lstrip("#").isdigit():
            return usage
        sticker_id, op, rest = int(args[0].lstrip("#")), args[1].lower(), " ".join(args[2:]).strip()
        current = await repo.by_id(sticker_id)
        if current is None:
            return Reply(t("commands.stickers.figurinha.unknown", id=sticker_id))
        if op in ("significado", "significa", "meaning"):
            if not rest:
                return usage
            updated = await repo.update(sticker_id, meaning=rest)
            return Reply(t("commands.stickers.figurinha.meaning_set", label=updated.label, meaning=rest))
        if op in ("acao", "ação", "action"):
            if not rest:
                return usage
            updated = await repo.update(sticker_id, action=rest)
            return Reply(t("commands.stickers.figurinha.action_set", emoji=updated.emoji, action=rest))
        if op in ("sem-acao", "sem-ação", "semacao", "no-action"):
            await repo.update(sticker_id, action="")
            return Reply(t("commands.stickers.figurinha.action_cleared", label=current.label))
        if op in ("remover", "apagar", "esquecer", "remove", "delete"):
            await repo.delete(sticker_id)
            return Reply(t("commands.stickers.figurinha.removed", label=current.label))
        return usage
