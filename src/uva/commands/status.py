"""/status: health of the services, uptime, queue and API spend."""

from __future__ import annotations

import asyncio

from uva.clock import format_duration, utcnow
from uva.commands.llm import describe_spend
from uva.commands.registry import CommandContext, CommandRegistry
from uva.services import Services

HEALTH_TIMEOUT_SECONDS = 5


async def collect_status(services: Services) -> list[str]:
    lines = ["Status da UVA"]
    lines.append(f"No ar há {format_duration(utcnow() - services.started_at)}")

    for name, check in services.health_checks.items():
        try:
            ok = await asyncio.wait_for(check(), HEALTH_TIMEOUT_SECONDS)
        except Exception:
            ok = False
        lines.append(f"{'🟢' if ok else '🔴'} {name}")

    lines.extend(await describe_spend(services))
    return lines


def register(registry: CommandRegistry) -> None:
    @registry.command(
        "status",
        "Serviços, uptime, fila e gastos",
        aliases=("estado",),
        examples=("status do sistema", "servicos no ar", "quanto gastei de api"),
    )
    async def status(ctx: CommandContext) -> str:
        return "\n".join(await collect_status(ctx.services))
