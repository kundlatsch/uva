"""/objetivos, /pausar, /retomar, /remover."""

from __future__ import annotations

from uva.clock import format_local, utcnow
from uva.commands.registry import CommandContext, CommandRegistry
from uva.goals.store import Goal, GoalStatus
from uva.services import Services

NO_GOALS = "Objetivos não estão configurados."
STATUS_LABEL = {
    GoalStatus.ATIVO: "▶️ ativo",
    GoalStatus.PAUSADO: "⏸ pausado",
    GoalStatus.CONCLUIDO: "✅ concluído",
}


def describe_goal(goal: Goal, services: Services) -> str:
    tz = services.tz
    lines = [
        f"{goal.id} {STATUS_LABEL[goal.status]} · prioridade {goal.priority} · {goal.kind.value}"
        f" · a cada {goal.min_interval_hours:g}h",
        f"   {goal.title}",
    ]
    if goal.status is GoalStatus.ATIVO:
        late = " (atrasada; roda assim que der)" if goal.next_run_at < utcnow() else ""
        lines.append(f"   próxima execução: {format_local(goal.next_run_at, tz, '%d/%m %H:%M')}{late}")
    if goal.last_attempt_at:
        when = format_local(goal.last_attempt_at, tz, "%d/%m %H:%M")
        lines.append(f"   última tentativa: {when} ({goal.last_attempt}) · execuções: {goal.run_count}")
    else:
        lines.append(f"   última tentativa: nunca · execuções: {goal.run_count}")
    if goal.needs_feature:
        lines.append(f"   precisa de: /feature {goal.needs_feature}")
    if goal.results:
        last = goal.results[-1]
        lines.append(f"   última ({format_local(last.at, tz, '%d/%m %H:%M')}): {last.summary[:150]}")
    return "\n".join(lines)


def register(registry: CommandRegistry) -> None:
    @registry.command(
        "objetivos",
        "Lista os objetivos em que trabalho sozinha",
        aliases=("goals", "metas"),
        examples=("meus objetivos", "quais sao os objetivos"),
    )
    async def goals(ctx: CommandContext) -> str:
        store = ctx.services.goals
        if store is None:
            return NO_GOALS
        items = await store.list()
        if not items:
            return (
                "Nenhum objetivo ainda. Peça algo como: \"crie um objetivo de acompanhar "
                "notícias sobre energia solar toda semana\"."
            )
        return "Objetivos:\n" + "\n".join(describe_goal(g, ctx.services) for g in items)

    async def _set_status(ctx: CommandContext, status: GoalStatus, verb: str) -> str:
        store = ctx.services.goals
        if store is None:
            return NO_GOALS
        if len(ctx.args) != 1:
            return f"Uso: /{verb} <id> (ids em /objetivos)"
        goal = await store.get(ctx.args[0])
        if goal is None:
            return f"Não achei o objetivo {ctx.args[0]}."
        await store.update(goal.id, status=status)
        return f"Objetivo {goal.id} ({goal.title}): {STATUS_LABEL[status]}."

    @registry.command("pausar", "Pausa um objetivo", aliases=("pause",), usage="<id>")
    async def pause(ctx: CommandContext) -> str:
        return await _set_status(ctx, GoalStatus.PAUSADO, "pausar")

    @registry.command("retomar", "Retoma um objetivo pausado", aliases=("resume",), usage="<id>")
    async def resume(ctx: CommandContext) -> str:
        return await _set_status(ctx, GoalStatus.ATIVO, "retomar")

    @registry.command(
        "remover", "Remove um objetivo (as notas ficam)", aliases=("remove",), usage="<id>"
    )
    async def remove(ctx: CommandContext) -> str:
        store = ctx.services.goals
        if store is None:
            return NO_GOALS
        if len(ctx.args) != 1:
            return "Uso: /remover <id> (ids em /objetivos)"
        if await store.delete(ctx.args[0]):
            return f"Objetivo {ctx.args[0]} removido. As notas dele continuam no second brain."
        return f"Não achei o objetivo {ctx.args[0]}."
