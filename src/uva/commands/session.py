"""Controlling the conversation itself: /parar, /refazer, /desfazer_turno, /modo."""

from __future__ import annotations

from datetime import timedelta

from uva import modes
from uva.clock import utcnow
from uva.commands.registry import CommandContext, CommandRegistry
from uva.i18n import t
from uva.replies import Button, Reply
from uva.tasks.queue import TaskStatus

# Background tasks started this recently can be stopped by /parar.
RECENT_TASKS = timedelta(minutes=15)
REDO_SKIP = ("/refazer", "/retry", "/desfazer_turno", "/undo_turn", "/parar", "/stop")


async def stoppable(services) -> list[tuple[str, str, str]]:
    """(kind, id, label) of what is running now: turns, then recent tasks."""
    found: list[tuple[str, str, str]] = []
    if services.turns is not None:
        found += [("turn", turn.id, turn.label) for turn in services.turns.running()]
    if services.tasks is not None:
        cutoff = utcnow() - RECENT_TASKS
        for record in await services.tasks.store.recent(20):
            if record.status in (TaskStatus.QUEUED, TaskStatus.RUNNING) and record.created_at >= cutoff:
                subject = record.params.get("query") or record.params.get("title") or ""
                found.append(("task", record.id, f"{record.action} {subject}".strip()))
    return found


async def stop_one(services, kind: str, item_id: str) -> str:
    if kind == "turn":
        turn = services.turns.stop(item_id) if services.turns is not None else None
        return t("commands.stop.stopped", label=turn.label) if turn else t("commands.stop.gone")
    if services.tasks is not None and await services.tasks.cancel(item_id):
        return t("commands.stop.stopped", label=item_id)
    return t("commands.stop.gone")


async def stop_callback(services, data: str) -> Reply:
    """The "stop:<kind>:<id>" / "stop:all" buttons."""
    _, _, rest = data.partition(":")
    if rest == "all":
        lines = [await stop_one(services, kind, item_id) for kind, item_id, _ in await stoppable(services)]
        return Reply("\n".join(lines) or t("commands.stop.nothing"))
    kind, _, item_id = rest.partition(":")
    return Reply(await stop_one(services, kind, item_id))


def register(registry: CommandRegistry) -> None:
    @registry.command(
        "parar",
        t("commands.stop.description"),
        aliases=("stop",),
        examples=("para tudo", "para o que voce esta fazendo"),
    )
    async def stop(ctx: CommandContext) -> Reply | str:
        running = await stoppable(ctx.services)
        if not running:
            return t("commands.stop.nothing")
        if len(running) == 1:
            kind, item_id, _ = running[0]
            return await stop_one(ctx.services, kind, item_id)
        buttons = [Button(f"⏹ {label[:30]}", f"stop:{kind}:{item_id}") for kind, item_id, label in running[:6]]
        buttons.append(Button(t("commands.stop.all"), "stop:all"))
        return Reply(t("commands.stop.which", count=len(running)), buttons=buttons)

    @registry.command(
        "refazer",
        t("commands.redo.description"),
        aliases=("retry", "de_novo"),
        examples=("refaz a ultima resposta",),
    )
    async def redo(ctx: CommandContext) -> Reply | str:
        text = await last_user_text(ctx.services)
        if text is None:
            return t("commands.redo.nothing")
        if ctx.reroute is None:
            return t("commands.redo.unavailable")
        return await ctx.reroute(text)

    @registry.command(
        "desfazer_turno",
        t("commands.undo_turn.description"),
        aliases=("undo_turn",),
    )
    async def undo_turn(ctx: CommandContext) -> str:
        services = ctx.services
        dropped = 0
        if services.stm is not None:
            dropped = await services.stm.drop_last_exchange()
        logged = await services.index.undo_last_exchange() if services.index is not None else []
        if not dropped and not logged:
            return t("commands.undo_turn.nothing")
        said = next((turn.text for turn in logged if turn.role == "user"), "")
        return t("commands.undo_turn.done", text=said[:120] or "…")

    @registry.command(
        "modo",
        t("commands.mode.description"),
        aliases=("mode",),
        usage=t("commands.mode.usage_arg"),
    )
    async def mode(ctx: CommandContext) -> str:
        services = ctx.services
        path = services.settings.repo_dir / "data" / "modes.json"
        if services.stm is None:
            return t("commands.mode.unavailable")
        if not ctx.args:
            current = (await services.stm.session()).mode
            return t("commands.mode.status", current=modes.label(current) if current else t("commands.mode.normal"),
                     modes=", ".join(modes.names(path)))
        name = " ".join(ctx.args)
        if name.lower() in modes.NORMAL:
            await services.stm.set_mode(None)
            return t("commands.mode.cleared")
        found = modes.find(name, path)
        if found is None:
            return t("commands.mode.unknown", name=name, modes=", ".join(modes.names(path)))
        await services.stm.set_mode(found[0])
        return t("commands.mode.set", name=modes.label(found[0]))


async def last_user_text(services) -> str | None:
    """The last thing the user said (not a /refazer itself)."""
    if services.stm is not None:
        try:
            session = await services.stm.session()
        except Exception:
            session = None
        if session is not None:
            for turn in reversed(session.turns):
                if turn.role == "user" and not turn.text.lower().startswith(REDO_SKIP):
                    return turn.text.removeprefix("[voice] ")
    if services.index is not None:
        for turn in reversed(await services.index.turns(limit=20)):
            if turn.role == "user" and not turn.text.lower().startswith(REDO_SKIP):
                return turn.text.removeprefix("[voice] ")
    return None
