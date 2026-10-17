"""/soul: show the personality file."""

from __future__ import annotations

from uva.commands.registry import CommandContext, CommandRegistry
from uva.i18n import t
from uva.replies import Reply
from uva.soul import Soul


def register(registry: CommandRegistry) -> None:
    @registry.command(
        "soul",
        t("commands.soul.soul.description"),
        aliases=("alma", "personalidade", "personality"),
        examples=("qual a sua personalidade",),
    )
    async def soul(ctx: CommandContext) -> Reply | str:
        path = ctx.services.settings.soul_file
        text = Soul(path).text()
        if not text:
            return t("commands.soul.soul.missing", path=path)
        return Reply(text=text, filename="soul.md").fit_for_telegram()
