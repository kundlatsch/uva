"""/lembrete and /lembretes: one-shot reminders (uva/reminders.py)."""

from __future__ import annotations

from uva import reminders
from uva.clock import utcnow
from uva.commands.registry import CommandContext, CommandRegistry
from uva.i18n import t
from uva.replies import Reply

# "/lembrete <id> <op>": Portuguese and English words -> canonical operation.
REMINDER_OPS = {
    "cancelar": "cancel", "cancel": "cancel", "remover": "cancel", "apagar": "cancel", "delete": "cancel",
    "adiar": "snooze", "snooze": "snooze", "soneca": "snooze", "postpone": "snooze",
}


def register(registry: CommandRegistry) -> None:
    @registry.command(
        "lembrete",
        t("commands.reminders.lembrete.description"),
        aliases=("reminder", "remindme"),
        usage=t("commands.reminders.lembrete.usage_arg"),
    )
    async def reminder(ctx: CommandContext) -> Reply | str:
        services = ctx.services
        if services.routines is None:
            return t("commands.routines.not_configured")
        args = ctx.args
        op = REMINDER_OPS.get(args[1].lower()) if len(args) >= 2 else None
        if op is not None and args[0].startswith("r"):
            if op == "cancel":
                return await reminders.cancel(services, args[0])
            when = " ".join(args[2:])
            try:
                found = reminders.parse_when(when, utcnow(), services.tz, snooze=True) if when else None
            except reminders.WhenError as exc:
                return t("reminders.bad_when", error=exc)
            if found is None or found.rest:
                return t("reminders.usage")
            return await reminders.snooze(services, args[0], found.at)
        text = (ctx.raw_args or " ".join(args)).strip()
        if not text:
            return t("reminders.usage")
        return await reminders.create_reminder(services, text)

    @registry.command(
        "lembretes",
        t("commands.reminders.lembretes.description"),
        aliases=("reminders",),
        examples=("meus lembretes", "quais lembretes eu tenho", "my reminders"),
    )
    async def reminder_list(ctx: CommandContext) -> Reply | str:
        if ctx.services.routines is None:
            return t("commands.routines.not_configured")
        body = await reminders.listing(ctx.services)
        return Reply.paginated(f"{body}\n\n{t('reminders.usage')}")
