"""/projeto and /projetos: new, separate projects built by Claude Code."""

from __future__ import annotations

from uva.commands.registry import CommandContext, CommandRegistry
from uva.logging.events import EventType, current_trace_id, emit
from uva.projects.service import ProjectError, describe_project
from uva.stm.refs import remember_ref

NOT_CONFIGURED = "Projetos não estão configurados (precisa de Mongo e do worker de projetos)."
RECENT = 10


async def request_project(services, description: str) -> str:
    """Shared by /projeto and the project_create action."""
    projects = services.projects
    if projects is None:
        return NOT_CONFIGURED
    try:
        record = await projects.request(description, current_trace_id(), services.tz)
    except ProjectError as exc:
        return str(exc)
    await emit(EventType.PROJECT, op="requested", project_id=record.id, slug=record.slug, description=description)
    await remember_ref(services, "projeto", record.id, f"projeto: {record.slug}", "na fila")
    s = services.settings
    return (
        f"🏗️ Beleza, vou criar o projeto {record.slug} ({record.id}) com o Claude Code, numa pasta "
        f"nova: {projects.host_path(record.slug)}\n"
        f"Limites: {s.project_timeout_minutes} min, US$ {s.project_max_budget_usd:.2f}. Te aviso quando "
        f"terminar. Acompanhe em /projetos; para parar, /cancelar {record.id}."
    )


def register(registry: CommandRegistry) -> None:
    @registry.command(
        "projeto",
        "Cria um projeto NOVO (fora do bot) com o Claude Code e avisa quando terminar",
        aliases=("novoprojeto",),
        usage="<descrição>",
    )
    async def project(ctx: CommandContext) -> str:
        description = " ".join(ctx.args).strip()
        if not description:
            return "Uso: /projeto <descrição>, ex.: /projeto um site estático de receitas com busca"
        return await request_project(ctx.services, description)

    @registry.command(
        "projetos",
        "Projetos que o Claude Code criou (ou está criando)",
        examples=("meus projetos", "como estao os projetos"),
    )
    async def projects(ctx: CommandContext) -> str:
        service = ctx.services.projects
        if service is None:
            return NOT_CONFIGURED
        records = await service.store.recent(RECENT)
        if not records:
            return "Nenhum projeto ainda. Peça com /projeto <descrição>."
        tz = ctx.services.tz
        lines = [describe_project(r, tz, service.host_path(r.slug)) for r in records]
        if problem := service.configured():
            lines.append(f"\n⚠️ {problem}")
        return "Projetos:\n" + "\n".join(lines)
