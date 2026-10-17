"""/projeto and /projetos: new, separate projects built by Claude Code."""

from __future__ import annotations

from uva.commands.registry import CommandContext, CommandRegistry
from uva.i18n import t
from uva.logging.events import EventType, current_trace_id, emit
from uva.projects.service import ProjectError, describe_project
from uva.stm.refs import remember_ref

RECENT = 10


async def request_project(services, description: str) -> str:
    """Shared by /projeto and the project_create action."""
    projects = services.projects
    if projects is None:
        return t("commands.projects.not_configured")
    try:
        record = await projects.request(description, current_trace_id(), services.tz)
    except ProjectError as exc:
        return str(exc)
    await emit(EventType.PROJECT, op="requested", project_id=record.id, slug=record.slug, description=description)
    await remember_ref(services, "projeto", record.id, f"project: {record.slug}", "na fila")
    s = services.settings
    return t(
        "commands.projects.requested",
        slug=record.slug,
        id=record.id,
        path=projects.host_path(record.slug),
        minutes=s.project_timeout_minutes,
        budget=s.project_max_budget_usd,
    )


def register(registry: CommandRegistry) -> None:
    @registry.command(
        "projeto",
        t("commands.projects.projeto.description"),
        aliases=("novoprojeto", "project", "newproject"),
        usage=t("commands.projects.projeto.usage_arg"),
    )
    async def project(ctx: CommandContext) -> str:
        description = " ".join(ctx.args).strip()
        if not description:
            return t("commands.projects.projeto.usage")
        return await request_project(ctx.services, description)

    @registry.command(
        "projetos",
        t("commands.projects.projetos.description"),
        aliases=("projects",),
        examples=("meus projetos", "como estao os projetos"),
    )
    async def projects(ctx: CommandContext) -> str:
        service = ctx.services.projects
        if service is None:
            return t("commands.projects.not_configured")
        records = await service.store.recent(RECENT)
        if not records:
            return t("commands.projects.projetos.none")
        tz = ctx.services.tz
        lines = [describe_project(r, tz, service.host_path(r.slug)) for r in records]
        if problem := service.configured():
            lines.append(f"\n⚠️ {problem}")
        return t("commands.projects.projetos.header") + "\n" + "\n".join(lines)
