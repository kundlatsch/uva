"""/rotina and /rotinas: recurring requests on a schedule."""

from __future__ import annotations

from uva.clock import format_local
from uva.commands.registry import CommandContext, CommandRegistry
from uva.i18n import t
from uva.replies import Reply
from uva.routines import describe, describe_schedule, parse, schedule, split_extras, valid_script

# "/rotina <id> <op>": Portuguese and English words -> canonical operation.
ROUTINE_OPS = {
    "pausar": "pausar", "pause": "pausar",
    "retomar": "retomar", "resume": "retomar",
    "remover": "remover", "apagar": "remover", "remove": "remover", "delete": "remover",
    "agora": "agora", "now": "agora",
    "habilidade": "skill", "skill": "skill",
    "incidentes": "incidents", "incidents": "incidents", "falhas": "incidents",
}
NO_SKILL = ("nenhuma", "nenhum", "none", "-")


async def create_routine(
    services, text: str, skill: str = "", script: str = "", context_from: str = ""
) -> str:
    """Shared by /rotina and the routine_create action."""
    if services.routines is None:
        return t("commands.routines.not_configured")
    try:
        routine = parse(text)
    except ValueError as exc:
        return t("commands.routines.bad_time", error=exc)
    if routine is None or len(routine.request) < 3:
        return t("commands.routines.usage")
    request, found_script, found_from = split_extras(routine.request)
    routine.request = request
    routine.script = (script or found_script).strip()
    routine.context_from = (context_from or found_from).strip().lower()
    if routine.script and not valid_script(routine.script):
        return t("commands.routines.rotina.bad_script", path=routine.script)
    if routine.context_from and await services.routines.get(routine.context_from) is None:
        return t("commands.routines.rotina.not_found", id=routine.context_from)
    if skill:
        if services.skills is None or services.skills.get(skill) is None:
            return t("commands.routines.rotina.unknown_skill", skill=skill)
        routine.skill = services.skills.get(skill).name
    routine = await schedule(services.routines, routine, services.tz)
    when = format_local(routine.next_run_at, services.tz, "%d/%m %H:%M")
    return t(
        "commands.routines.created",
        id=routine.id,
        schedule=describe_schedule(routine),
        request=routine.request,
        when=when,
    )


def register(registry: CommandRegistry) -> None:
    @registry.command(
        "rotina",
        t("commands.routines.rotina.description"),
        aliases=("agendar", "routine", "schedule"),
        usage=t("commands.routines.rotina.usage_arg"),
    )
    async def routine(ctx: CommandContext) -> Reply | str:
        store = ctx.services.routines
        if store is None:
            return t("commands.routines.not_configured")
        args = ctx.args
        op = ROUTINE_OPS.get(args[1].lower()) if len(args) >= 2 else None
        if op is not None and args[0].startswith("r"):
            current = await store.get(args[0])
            if current is None:
                return t("commands.routines.rotina.not_found", id=args[0])
            if op == "pausar":
                await store.update(current.id, enabled=False)
                return t("commands.routines.rotina.paused", id=current.id)
            if op == "retomar":
                from uva.routines import next_run

                await store.update(current.id, enabled=True, next_run_at=next_run(current, _now(), ctx.services.tz))
                return t("commands.routines.rotina.resumed", id=current.id)
            if op == "remover":
                await store.delete(current.id)
                return t("commands.routines.rotina.removed", id=current.id)
            if op == "skill":
                return await _set_skill(ctx, current, args[2] if len(args) > 2 else "")
            if op == "incidents":
                return await _incidents(ctx, current)
            await store.update(current.id, next_run_at=_now())
            return t("commands.routines.rotina.run_now", id=current.id)
        text = (ctx.raw_args or " ".join(args)).strip()
        if not text:
            return t("commands.routines.usage")
        return await create_routine(ctx.services, text)

    @registry.command(
        "rotinas",
        t("commands.routines.rotinas.description"),
        aliases=("routines",),
        examples=("minhas rotinas", "o que voce faz todo dia"),
    )
    async def routines(ctx: CommandContext) -> Reply | str:
        store = ctx.services.routines
        if store is None:
            return t("commands.routines.not_configured")
        items = await store.all()
        if not items:
            return t("commands.routines.rotinas.none")
        body = "\n".join(describe(r, ctx.services.tz) for r in items)
        return Reply.paginated(
            f"{body}\n\n{t('commands.routines.usage')}",
            header=t("commands.routines.rotinas.header", count=len(items)),
        )


async def _incidents(ctx: CommandContext, routine) -> str:
    if ctx.services.sqlite is None:
        return t("commands.routines.rotina.no_incidents", id=routine.id)
    from uva.incidents import IncidentStore

    found = await IncidentStore(ctx.services.sqlite).list(routine.id)
    if not found:
        return t("commands.routines.rotina.no_incidents", id=routine.id)
    tz = ctx.services.tz
    lines = [t("commands.routines.rotina.incidents_header", id=routine.id)]
    for incident in found:
        state = t(f"commands.routines.rotina.incident_state.{incident.state}")
        lines.append(t(
            "commands.routines.rotina.incident_line", state=state, count=incident.count,
            first=format_local(incident.first_at, tz, "%d/%m %H:%M"), error=incident.last_error[:200],
        ))
    return "\n".join(lines)


async def _set_skill(ctx: CommandContext, routine, name: str) -> str:
    store = ctx.services.routines
    if not name or name.lower() in NO_SKILL:
        await store.update(routine.id, skill="")
        return t("commands.routines.rotina.skill_cleared", id=routine.id)
    skill = ctx.services.skills.get(name) if ctx.services.skills is not None else None
    if skill is None:
        return t("commands.routines.rotina.unknown_skill", skill=name)
    await store.update(routine.id, skill=skill.name)
    return t("commands.routines.rotina.skill_set", id=routine.id, skill=skill.name)


def _now():
    from uva.clock import utcnow

    return utcnow()
