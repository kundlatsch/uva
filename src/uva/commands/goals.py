"""/objetivos, /pausar, /retomar, /remover."""

from __future__ import annotations

from uva.clock import format_local, utcnow
from uva.commands.registry import CommandContext, CommandRegistry
from uva.goals.store import Goal, GoalStatus
from uva.i18n import t
from uva.services import Services


def status_label(status: GoalStatus) -> str:
    """'▶️ ativo' / '▶️ active' (the stored value stays Portuguese)."""
    return t(f"commands.goals.status.{status.value}")


def describe_goal(goal: Goal, services: Services) -> str:
    tz = services.tz
    lines = [
        t(
            "commands.goals.describe.head",
            id=goal.id,
            status=status_label(goal.status),
            priority=goal.priority,
            kind=goal.kind.value,
            hours=f"{goal.min_interval_hours:g}",
        ),
        f"   {goal.title}",
    ]
    if goal.effective_interval_hours and goal.effective_interval_hours != goal.min_interval_hours:
        lines.append(t("commands.goals.describe.stretched", hours=f"{goal.effective_interval_hours:g}",
                       runs=goal.unchanged_runs + 1))
    if goal.verify:
        lines.append(t("commands.goals.describe.verify", verify=goal.verify))
    if goal.stop_when:
        lines.append(t("commands.goals.describe.stop_when", stop_when=goal.stop_when))
    if goal.skill:
        lines.append(t("commands.goals.describe.skill", skill=goal.skill))
    if goal.status is GoalStatus.ATIVO:
        late = t("commands.goals.describe.late") if goal.next_run_at < utcnow() else ""
        when = format_local(goal.next_run_at, tz, "%d/%m %H:%M")
        lines.append(t("commands.goals.describe.next_run", when=when, late=late))
    if goal.last_attempt_at:
        when = format_local(goal.last_attempt_at, tz, "%d/%m %H:%M")
        lines.append(
            t(
                "commands.goals.describe.last_attempt",
                when=when,
                attempt=goal.last_attempt,
                runs=goal.run_count,
            )
        )
    else:
        lines.append(t("commands.goals.describe.never", runs=goal.run_count))
    if goal.needs_feature:
        lines.append(t("commands.goals.describe.needs_feature", feature=goal.needs_feature))
    if goal.results:
        last = goal.results[-1]
        when = format_local(last.at, tz, "%d/%m %H:%M")
        lines.append(t("commands.goals.describe.last_result", when=when, summary=last.summary[:150]))
    return "\n".join(lines)


def register(registry: CommandRegistry) -> None:
    @registry.command(
        "objetivos",
        t("commands.goals.objetivos.description"),
        aliases=("goals", "metas"),
        examples=("meus objetivos", "quais sao os objetivos"),
    )
    async def goals(ctx: CommandContext) -> str:
        store = ctx.services.goals
        if store is None:
            return t("commands.goals.not_configured")
        items = await store.list()
        if not items:
            return t("commands.goals.objetivos.none")
        return t("commands.goals.objetivos.header") + "\n" + "\n".join(
            describe_goal(g, ctx.services) for g in items
        )

    async def _set_status(ctx: CommandContext, status: GoalStatus, verb: str) -> str:
        store = ctx.services.goals
        if store is None:
            return t("commands.goals.not_configured")
        if len(ctx.args) != 1:
            return t(f"commands.goals.{verb}.usage")
        goal = await store.get(ctx.args[0])
        if goal is None:
            return t("commands.goals.not_found", id=ctx.args[0])
        await store.update(goal.id, status=status)
        return t("commands.goals.status_changed", id=goal.id, title=goal.title, status=status_label(status))

    @registry.command(
        "pausar", t("commands.goals.pausar.description"), aliases=("pause",), usage="<id>"
    )
    async def pause(ctx: CommandContext) -> str:
        return await _set_status(ctx, GoalStatus.PAUSADO, "pausar")

    @registry.command(
        "retomar", t("commands.goals.retomar.description"), aliases=("resume",), usage="<id>"
    )
    async def resume(ctx: CommandContext) -> str:
        return await _set_status(ctx, GoalStatus.ATIVO, "retomar")

    @registry.command(
        "remover", t("commands.goals.remover.description"), aliases=("remove",), usage="<id>"
    )
    async def remove(ctx: CommandContext) -> str:
        store = ctx.services.goals
        if store is None:
            return t("commands.goals.not_configured")
        if len(ctx.args) != 1:
            return t("commands.goals.remover.usage")
        if await store.delete(ctx.args[0]):
            return t("commands.goals.remover.removed", id=ctx.args[0])
        return t("commands.goals.not_found", id=ctx.args[0])
