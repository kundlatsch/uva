"""/intencoes and /intencao: standing intents (uva/intents.py)."""

from __future__ import annotations

from uva.clock import format_local
from uva.commands.registry import CommandContext, CommandRegistry
from uva.i18n import t
from uva.intents import store_for
from uva.replies import Reply

CANCEL_WORDS = ("cancelar", "cancel", "remover", "apagar", "delete")


def describe(intent, tz) -> str:
    state = t(f"intents.state.{intent.state}")
    scope = " " + t("intents.list.results") if intent.scope == "results" else ""
    head = t(
        "intents.list.line", id=intent.id, state=state, fires=intent.fires, max=intent.max_fires,
        text=intent.text[:150], scope=scope,
    )
    when = format_local(intent.created_at, tz, "%d/%m/%Y")
    return f"{head}\n   {t('intents.list.triggers', triggers=intent.describe_triggers(), created=when)}"


def register(registry: CommandRegistry) -> None:
    @registry.command(
        "intencoes",
        t("commands.intents.intencoes.description"),
        aliases=("intenções", "intents"),
        examples=("minhas intencoes", "my intents"),
    )
    async def intents(ctx: CommandContext) -> Reply | str:
        store = store_for(ctx.services)
        if store is None:
            return t("intents.no_storage")
        found = await store.list()
        if not found:
            return t("intents.list.none")
        live = [i for i in found if i.state in ("armed", "fired")]
        closed = [i for i in found if i.state not in ("armed", "fired")][-10:]
        lines = [t("intents.list.header", count=len(live))]
        lines += [describe(i, ctx.services.tz) for i in live] or [t("intents.list.no_live")]
        if closed:
            lines += ["", t("intents.list.closed")] + [describe(i, ctx.services.tz) for i in closed]
        return Reply.paginated("\n".join(lines) + "\n\n" + t("intents.usage"))

    @registry.command(
        "intencao",
        t("commands.intents.intencao.description"),
        aliases=("intenção", "intent"),
        usage=t("commands.intents.intencao.usage_arg"),
    )
    async def intent(ctx: CommandContext) -> str:
        store = store_for(ctx.services)
        if store is None:
            return t("intents.no_storage")
        args = ctx.args
        if len(args) < 2 or not args[0].lstrip("#").isdigit() or args[1].lower() not in CANCEL_WORDS:
            return t("intents.usage")
        intent_id = int(args[0].lstrip("#"))
        cancelled = await store.cancel(intent_id)
        if cancelled is None:
            return t("intents.not_found", id=intent_id)
        return t("intents.cancelled", id=cancelled.id, text=cancelled.text)
