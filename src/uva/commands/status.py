"""/status: health of the services, uptime, queue and API spend."""

from __future__ import annotations

import asyncio

from uva.clock import format_duration, utcnow
from uva.commands.llm import describe_spend
from uva.commands.registry import CommandContext, CommandRegistry
from uva.i18n import t
from uva.services import Services

HEALTH_TIMEOUT_SECONDS = 5


async def collect_status(services: Services) -> list[str]:
    lines = [t("commands.status.title")]
    lines.append(t("commands.status.uptime", duration=format_duration(utcnow() - services.started_at)))

    for name, check in services.health_checks.items():
        try:
            ok = await asyncio.wait_for(check(), HEALTH_TIMEOUT_SECONDS)
        except Exception:
            ok = False
        lines.append(f"{'🟢' if ok else '🔴'} {name}")

    if services.tasks is not None:
        try:
            size = await services.tasks.queue_size()
            shown = size if size is not None else t("commands.status.queue_unknown")
            lines.append(t("commands.status.queue", size=shown))
        except Exception:
            lines.append(t("commands.status.queue_unavailable"))

    if services.mcp is not None:
        from uva.commands.mcp import status_line

        try:
            line = await asyncio.wait_for(status_line(services), HEALTH_TIMEOUT_SECONDS)
        except Exception:
            line = t("commands.mcp.status_down")
        if line:
            lines.append(line)

    if services.vectors is not None:
        try:
            lines.append(t("commands.status.vectors", count=await services.vectors.count()))
        except Exception:
            lines.append(t("commands.status.vectors_unavailable"))

    lines.extend(await describe_spend(services))
    from uva.profile import usage

    lines += [u.line() for u in usage(services)]
    return lines


def register(registry: CommandRegistry) -> None:
    @registry.command(
        "status",
        t("commands.status.status.description"),
        aliases=("estado",),
        examples=("status do sistema", "servicos no ar", "quanto gastei de api"),
    )
    async def status(ctx: CommandContext) -> str:
        return "\n".join(await collect_status(ctx.services))
