"""/tarefas and /cancelar: background tasks."""

from __future__ import annotations

from uva.commands.registry import CommandContext, CommandRegistry
from uva.tasks.queue import describe_task

RECENT_LIMIT = 10


def register(registry: CommandRegistry) -> None:
    @registry.command(
        "tarefas",
        "Tarefas em segundo plano recentes",
        aliases=("tasks", "fila", "queue"),
        examples=("minhas tarefas", "o que esta rodando"),
    )
    async def tasks(ctx: CommandContext) -> str:
        queue = ctx.services.tasks
        if queue is None:
            return "Tarefas em segundo plano não estão configuradas (Redis)."
        records = await queue.store.recent(RECENT_LIMIT)
        if not records:
            return "Nenhuma tarefa recente."
        return "Tarefas recentes:\n" + "\n".join(describe_task(r) for r in records)

    @registry.command("cancelar", "Cancela uma tarefa", aliases=("cancel",), usage="<id>")
    async def cancel(ctx: CommandContext) -> str:
        if len(ctx.args) != 1:
            return "Uso: /cancelar <id> (veja os ids em /tarefas ou /projetos)"
        task_id = ctx.args[0]
        projects = ctx.services.projects
        if projects is not None and (project := await projects.cancel(task_id)) is not None:
            return f"🛑 Parando o projeto {project.slug} ({task_id}). Te aviso quando parar."
        queue = ctx.services.tasks
        if queue is None:
            return "Tarefas em segundo plano não estão configuradas (Redis)."
        if await queue.cancel(task_id):
            return f"{ctx.services.say('cancelado', id=task_id)}\n(tarefa {task_id} cancelada)"
        return f"Não achei a tarefa {task_id} em andamento (/tarefas)."
