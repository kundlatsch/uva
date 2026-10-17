"""/rotina and /rotinas: recurring requests on a schedule."""

from __future__ import annotations

from uva.clock import format_local
from uva.commands.registry import CommandContext, CommandRegistry
from uva.replies import Reply
from uva.routines import describe, describe_schedule, parse, schedule

NOT_CONFIGURED = "Rotinas precisam do Mongo."
USAGE = (
    "Uso:\n/rotina todo dia 08:00 <pedido>\n/rotina segunda e quinta 9h <pedido>\n"
    "/rotina a cada 6 horas <pedido>\n/rotinas — lista\n/rotina <id> pausar|retomar|remover|agora"
)
# "/rotina <id> <op>": Portuguese and English words -> canonical operation.
ROUTINE_OPS = {
    "pausar": "pausar", "pause": "pausar",
    "retomar": "retomar", "resume": "retomar",
    "remover": "remover", "apagar": "remover", "remove": "remover", "delete": "remover",
    "agora": "agora", "now": "agora",
}


async def create_routine(services, text: str) -> str:
    """Shared by /rotina and the routine_create action."""
    if services.routines is None:
        return NOT_CONFIGURED
    try:
        routine = parse(text)
    except ValueError as exc:
        return f"Não entendi o horário: {exc}."
    if routine is None or len(routine.request) < 3:
        return USAGE
    routine = await schedule(services.routines, routine, services.tz)
    when = format_local(routine.next_run_at, services.tz, "%d/%m %H:%M")
    return (
        f"⏰ Rotina {routine.id} criada: {describe_schedule(routine)}\n“{routine.request}”\n"
        f"Primeira vez: {when}. Se o PC estiver desligado na hora, rodo uma vez quando ele ligar."
    )


def register(registry: CommandRegistry) -> None:
    @registry.command(
        "rotina",
        "Cria uma tarefa recorrente (todo dia 08:00 ..., segunda 9h ..., a cada 6 horas ...)",
        aliases=("agendar", "routine", "schedule"),
        usage="<quando> <pedido> | <id> pausar|retomar|remover|agora",
    )
    async def routine(ctx: CommandContext) -> Reply | str:
        store = ctx.services.routines
        if store is None:
            return NOT_CONFIGURED
        args = ctx.args
        op = ROUTINE_OPS.get(args[1].lower()) if len(args) >= 2 else None
        if op is not None and args[0].startswith("r"):
            current = await store.get(args[0])
            if current is None:
                return f"Não achei a rotina {args[0]} (/rotinas)."
            if op == "pausar":
                await store.update(current.id, enabled=False)
                return f"⏸️ Rotina {current.id} pausada."
            if op == "retomar":
                from uva.routines import next_run

                await store.update(current.id, enabled=True, next_run_at=next_run(current, _now(), ctx.services.tz))
                return f"▶️ Rotina {current.id} retomada."
            if op == "remover":
                await store.delete(current.id)
                return f"🗑️ Rotina {current.id} removida."
            await store.update(current.id, next_run_at=_now())
            return f"⏰ Rotina {current.id} vai rodar no próximo minuto."
        text = (ctx.raw_args or " ".join(args)).strip()
        if not text:
            return USAGE
        return await create_routine(ctx.services, text)

    @registry.command(
        "rotinas",
        "Tarefas recorrentes agendadas",
        aliases=("routines",),
        examples=("minhas rotinas", "o que voce faz todo dia"),
    )
    async def routines(ctx: CommandContext) -> Reply | str:
        store = ctx.services.routines
        if store is None:
            return NOT_CONFIGURED
        items = await store.all()
        if not items:
            return "Nenhuma rotina. Crie com /rotina todo dia 08:00 <pedido>."
        body = "\n".join(describe(r, ctx.services.tz) for r in items)
        return Reply.paginated(f"{body}\n\n{USAGE}", header=f"⏰ Rotinas ({len(items)})")


def _now():
    from uva.clock import utcnow

    return utcnow()
