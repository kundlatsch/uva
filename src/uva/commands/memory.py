"""/memoria, /lembrar and /esquecer."""

from __future__ import annotations

from uva.clock import format_local
from uva.commands.registry import CommandContext, CommandRegistry
from uva.logging.events import EventType, current_trace_id, emit
from uva.memory.store import MemoryCategory

NO_MEMORY = "A memória não está configurada."


def register(registry: CommandRegistry) -> None:
    @registry.command(
        "memoria",
        "Mostra o que eu lembro (ou uma chave)",
        aliases=("memória", "memorias"),
        usage="[chave]",
        examples=("o que voce sabe sobre mim", "o que voce lembra de mim"),
    )
    async def memory(ctx: CommandContext) -> str:
        service = ctx.services.memory
        if service is None:
            return NO_MEMORY
        tz = ctx.services.tz
        if ctx.args:
            item = await service.get(" ".join(ctx.args))
            if item is None:
                return f"Não lembro de nada com a chave '{' '.join(ctx.args)}'."
            return (
                f"{item.key} [{item.category.value}]: {item.value}\n"
                f"Relevância {item.relevance:.2f}, usada {item.use_count}x, "
                f"último uso {format_local(item.last_used_at, tz, '%d/%m/%Y')}"
            )

        ranked = await service.ranked()
        if not ranked:
            return "Ainda não guardei nada. Use /lembrar <chave> <valor>."
        working = {i.key for i in await service.working_memory()}
        lines = [f"Memória: {len(ranked)} itens (★ = memória de trabalho)"]
        for item, value in ranked[:30]:
            star = "★" if item.key in working else " "
            lines.append(f"{star} {item.key}: {item.value} [{item.category.value}, nota {value:.2f}]")
        return "\n".join(lines)

    @registry.command(
        "lembrar",
        "Guarda uma memória",
        aliases=("lembra",),
        usage="<chave> <valor>",
    )
    async def remember(ctx: CommandContext) -> str:
        service = ctx.services.memory
        if service is None:
            return NO_MEMORY
        if len(ctx.args) < 2:
            return "Uso: /lembrar <chave> <valor> (ex.: /lembrar cidade Florianópolis)"
        key, value = ctx.args[0], " ".join(ctx.args[1:])
        # Explicit requests from the user are important by definition.
        item = await service.save(
            key, value, category=MemoryCategory.FATO, relevance=0.8, source=current_trace_id()
        )
        await emit(EventType.MEMORY, op="save", key=item.key, value=item.value, by="user")
        return f"Ok, vou lembrar: {item.key} = {item.value}"

    @registry.command("esquecer", "Apaga uma memória", usage="<chave>")
    async def forget(ctx: CommandContext) -> str:
        service = ctx.services.memory
        if service is None:
            return NO_MEMORY
        if not ctx.args:
            return "Uso: /esquecer <chave> (veja as chaves em /memoria)"
        key = " ".join(ctx.args)
        removed = await service.forget(key)
        await emit(EventType.MEMORY, op="forget", key=key, removed=removed, by="user")
        return f"Esqueci '{key}'." if removed else f"Não achei '{key}' na memória."
