"""/figurinhas and /figurinha: what the bot learned about each sticker."""

from __future__ import annotations

from uva.commands.registry import CommandContext, CommandRegistry
from uva.replies import Reply
from uva.stickers import describe

NOT_CONFIGURED = "Figurinhas precisam do SQLite."
USAGE = (
    "Uso:\n/figurinhas — lista\n/figurinha <n> significado <texto>\n/figurinha <n> acao <pedido>\n"
    "/figurinha <n> sem-acao\n/figurinha <n> remover"
)


def register(registry: CommandRegistry) -> None:
    @registry.command(
        "figurinhas",
        "Figurinhas que eu conheço: significado e ação de cada uma",
        aliases=("stickers",),
        examples=("quais figurinhas voce conhece",),
    )
    async def stickers(ctx: CommandContext) -> Reply:
        repo = ctx.services.stickers
        if repo is None:
            return Reply(NOT_CONFIGURED)
        items = await repo.all()
        if not items:
            return Reply("Ainda não conheço nenhuma figurinha. Manda uma que eu pergunto o que significa.")
        body = "\n".join(describe(s) for s in items)
        return Reply.paginated(f"{body}\n\n{USAGE}", header=f"🏷️ Figurinhas ({len(items)})")

    @registry.command(
        "figurinha",
        "Edita uma figurinha: significado, ação ou remover",
        usage="<n> significado|acao|sem-acao|remover [texto]",
    )
    async def sticker(ctx: CommandContext) -> Reply:
        repo = ctx.services.stickers
        if repo is None:
            return Reply(NOT_CONFIGURED)
        args = ctx.args
        if len(args) < 2 or not args[0].lstrip("#").isdigit():
            return Reply(USAGE)
        sticker_id, op, rest = int(args[0].lstrip("#")), args[1].lower(), " ".join(args[2:]).strip()
        current = await repo.by_id(sticker_id)
        if current is None:
            return Reply(f"Não conheço a figurinha #{sticker_id} (/figurinhas).")
        if op in ("significado", "significa"):
            if not rest:
                return Reply(USAGE)
            updated = await repo.update(sticker_id, meaning=rest)
            return Reply(f"✅ {updated.label} agora significa: {rest}")
        if op in ("acao", "ação"):
            if not rest:
                return Reply(USAGE)
            updated = await repo.update(sticker_id, action=rest)
            return Reply(f"✅ Toda vez que você mandar {updated.emoji}, eu faço: {rest}")
        if op in ("sem-acao", "sem-ação", "semacao"):
            await repo.update(sticker_id, action="")
            return Reply(f"✅ {current.label} não faz mais nada sozinha (o significado continua).")
        if op in ("remover", "apagar", "esquecer"):
            await repo.delete(sticker_id)
            return Reply(f"🗑️ Esqueci a figurinha {current.label} (significado e ação).")
        return Reply(USAGE)
