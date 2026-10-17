"""/tarefas and /cancelar: background tasks."""

from __future__ import annotations

from uva.commands.registry import CommandContext, CommandRegistry
from uva.i18n import t
from uva.tasks.queue import describe_task

RECENT_LIMIT = 10


def register(registry: CommandRegistry) -> None:
    @registry.command(
        "tarefas",
        t("commands.tasks.tarefas.description"),
        aliases=("tasks", "fila", "queue"),
        examples=("minhas tarefas", "o que esta rodando"),
    )
    async def tasks(ctx: CommandContext) -> str:
        queue = ctx.services.tasks
        if queue is None:
            return t("commands.tasks.not_configured")
        records = await queue.store.recent(RECENT_LIMIT)
        if not records:
            return t("commands.tasks.none")
        return t("commands.tasks.recent") + "\n" + "\n".join(describe_task(r) for r in records)

    @registry.command(
        "cancelar", t("commands.tasks.cancelar.description"), aliases=("cancel",), usage="<id>"
    )
    async def cancel(ctx: CommandContext) -> str:
        if len(ctx.args) != 1:
            return t("commands.tasks.cancelar.usage")
        task_id = ctx.args[0]
        projects = ctx.services.projects
        if projects is not None and (project := await projects.cancel(task_id)) is not None:
            return t("commands.tasks.cancelar.stopping_project", slug=project.slug, id=task_id)
        queue = ctx.services.tasks
        if queue is None:
            return t("commands.tasks.not_configured")
        if await queue.cancel(task_id):
            return t(
                "commands.tasks.cancelar.cancelled",
                phrase=ctx.services.say("cancelado", id=task_id),
                id=task_id,
            )
        return t("commands.tasks.cancelar.not_found", id=task_id)
