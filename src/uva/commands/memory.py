"""/memoria, /lembrar and /esquecer."""

from __future__ import annotations

from uva.clock import format_local
from uva.commands.registry import CommandContext, CommandRegistry
from uva.i18n import t
from uva.logging.events import EventType, current_trace_id, emit
from uva.memory.store import MemoryCategory


def register(registry: CommandRegistry) -> None:
    @registry.command(
        "memoria",
        t("commands.memory.memoria.description"),
        aliases=("memória", "memorias", "memory"),
        usage=t("commands.memory.memoria.usage_arg"),
        examples=("o que voce sabe sobre mim", "o que voce lembra de mim"),
    )
    async def memory(ctx: CommandContext) -> str:
        service = ctx.services.memory
        if service is None:
            return t("commands.memory.not_configured")
        tz = ctx.services.tz
        if ctx.args:
            item = await service.get(" ".join(ctx.args))
            if item is None:
                return t("commands.memory.memoria.unknown_key", key=" ".join(ctx.args))
            await service.touch([item.key])  # reading it is a use
            return t(
                "commands.memory.memoria.item",
                key=item.key,
                category=item.category.value,
                value=item.value,
                relevance=item.relevance,
                uses=item.use_count,
                last_used=format_local(item.last_used_at, tz, "%d/%m/%Y"),
            )

        ranked = await service.ranked()
        if not ranked:
            return t("commands.memory.memoria.empty")
        working = {i.key for i in await service.working_memory()}
        lines = [t("commands.memory.memoria.header", count=len(ranked))]
        for item, value in ranked[:30]:
            star = "★" if item.key in working else " "
            lines.append(
                t(
                    "commands.memory.memoria.line",
                    star=star,
                    key=item.key,
                    value=item.value,
                    category=item.category.value,
                    score=value,
                )
            )
        return "\n".join(lines)

    @registry.command(
        "lembrar",
        t("commands.memory.lembrar.description"),
        aliases=("lembra", "remember"),
        usage=t("commands.memory.lembrar.usage_arg"),
    )
    async def remember(ctx: CommandContext) -> str:
        service = ctx.services.memory
        if service is None:
            return t("commands.memory.not_configured")
        if len(ctx.args) < 2:
            return t("commands.memory.lembrar.usage")
        key, value = ctx.args[0], " ".join(ctx.args[1:])
        # Explicit requests from the user are important by definition.
        item = await service.save(
            key, value, category=MemoryCategory.FATO, relevance=0.8, source=current_trace_id()
        )
        await emit(EventType.MEMORY, op="save", key=item.key, value=item.value, by="user")
        return t("commands.memory.lembrar.saved", key=item.key, value=item.value)

    @registry.command(
        "esquecer", t("commands.memory.esquecer.description"), aliases=("forget",), usage="<chave>"
    )
    async def forget(ctx: CommandContext) -> str:
        service = ctx.services.memory
        if service is None:
            return t("commands.memory.not_configured")
        if not ctx.args:
            return t("commands.memory.esquecer.usage")
        key = " ".join(ctx.args)
        removed = await service.forget(key)
        await emit(EventType.MEMORY, op="forget", key=key, removed=removed, by="user")
        if removed:
            return t("commands.memory.esquecer.forgotten", key=key)
        return t("commands.memory.esquecer.not_found", key=key)
