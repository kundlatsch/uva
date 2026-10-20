"""/aparte (a side question), /custo (cost footer) and /doctor (setup check)."""

from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path

from uva.commands.registry import CommandContext, CommandRegistry
from uva.i18n import t
from uva.replies import Reply

# Never kept in the short-term memory, the conversation log or the review.
ASIDE_COMMANDS = ("/aparte", "/btw")
ON_WORDS = ("on", "ligar", "liga", "sim")
OFF_WORDS = ("off", "desligar", "desliga", "nao", "não")
DISK_MIN_FREE_GB = 1.0
_ENV_KEY = re.compile(r"^\s*([A-Z][A-Z0-9_]*)\s*=", re.MULTILINE)


async def aside(ctx: CommandContext, question: str) -> Reply:
    from uva.llm.base import LLMError, LLMMessage
    from uva.llm.budget import Scope
    from uva.llm.prompts import aside_prompt

    services = ctx.services
    if services.llm is None:
        return Reply(t("commands.extras.aparte.no_llm"))
    if services.budget is not None and not (status := await services.budget.check(Scope.INTERACTIVE)).allowed:
        return Reply(f"{services.say('sem_credito')}\n({status.reason})")
    history = []
    if services.stm is not None:
        try:
            history = services.stm.history(await services.stm.session(), services.settings.llm_history_messages)
        except Exception:
            history = []
    try:
        response = await services.llm.complete(
            [*history, LLMMessage("user", question)], purpose="aside", scope=Scope.INTERACTIVE,
            system=aside_prompt(), max_tokens=800,
        )
    except LLMError:
        return Reply(services.say("llm_erro"), failed=True)
    return Reply(t("commands.extras.aparte.reply", text=response.text.strip()))


async def doctor(services) -> list[str]:
    """One line per check: ✅ fine, or ⚠️ the problem and how to fix it."""
    lines = [t("commands.extras.doctor.title")]

    def ok(key: str, **values) -> None:
        lines.append("✅ " + t(f"commands.extras.doctor.ok.{key}", **values))

    def bad(key: str, **values) -> None:
        lines.append("⚠️ " + t(f"commands.extras.doctor.problem.{key}", **values))

    settings = services.settings
    repo = settings.repo_dir
    example, env = repo / ".env.example", repo / ".env"
    if example.exists() and env.exists():
        known = set(_ENV_KEY.findall(example.read_text(encoding="utf-8")))
        used = set(_ENV_KEY.findall(env.read_text(encoding="utf-8")))
        if unknown := sorted(used - known):
            bad("env_unknown", keys=", ".join(unknown[:10]))
        else:
            ok("env", missing=len(known - used))
    else:
        bad("env_missing")

    for name, check in services.health_checks.items():
        try:
            up = await asyncio.wait_for(check(), 5)
        except Exception:
            up = False
        (ok if up else bad)("service", name=name)

    if services.index is not None and services.vault is not None:
        try:
            indexed = await services.index.count("note")
            notes = await asyncio.to_thread(lambda: sum(1 for _ in services.vault.iter_notes()))
            (ok if indexed == notes else bad)("index", indexed=indexed, notes=notes)
        except Exception as exc:
            bad("index_error", error=str(exc)[:120])
    if services.vectors is not None:
        try:
            ok("vectors", count=await services.vectors.count())
        except Exception as exc:
            bad("vectors", error=str(exc)[:120])

    from uva.profile import usage

    for item in usage(services):
        (bad if item.over else ok)("size", name=item.name, chars=item.chars, limit=item.limit)

    if services.mcp is not None:
        if services.mcp.config_error:
            bad("mcp_config", error=services.mcp.config_error[:150])
        else:
            try:
                from uva.commands.mcp import status_line

                line = await asyncio.wait_for(status_line(services), 5)
                ok("mcp", line=line or "—")
            except Exception as exc:
                bad("mcp_down", error=str(exc)[:120])

    for label, path in (("sqlite", Path(settings.sqlite_path).parent), ("brain", Path(settings.second_brain_path)),
                        ("repo", repo)):
        try:
            disk = await asyncio.to_thread(shutil.disk_usage, path)
        except OSError:
            continue
        free_gb = disk.free / 1024**3
        used_pct = round(100 * disk.used / max(1, disk.total))
        (ok if free_gb >= DISK_MIN_FREE_GB and used_pct < 90 else bad)(
            "disk", name=label, free=f"{free_gb:.1f}", used=used_pct,
        )
    return lines


def register(registry: CommandRegistry) -> None:
    @registry.command(
        "aparte",
        t("commands.extras.aparte.description"),
        aliases=("btw",),
        usage=t("commands.extras.aparte.usage_arg"),
    )
    async def aparte(ctx: CommandContext) -> Reply | str:
        question = (ctx.raw_args or " ".join(ctx.args)).strip()
        if not question:
            return t("commands.extras.aparte.usage")
        return await aside(ctx, question)

    @registry.command(
        "custo",
        t("commands.extras.custo.description"),
        aliases=("usage", "cost"),
        usage=t("commands.extras.custo.usage_arg"),
    )
    async def custo(ctx: CommandContext) -> str:
        stm = ctx.services.stm
        if stm is None:
            return t("commands.extras.custo.no_stm")
        word = ctx.args[0].lower() if ctx.args else ""
        if word not in ON_WORDS + OFF_WORDS:
            on = (await stm.session()).show_cost
            return t("commands.extras.custo.state_on" if on else "commands.extras.custo.state_off")
        on = word in ON_WORDS
        await stm.set_show_cost(on)
        return t("commands.extras.custo.on" if on else "commands.extras.custo.off")

    @registry.command(
        "doctor",
        t("commands.extras.doctor.description"),
        aliases=("diagnostico", "diagnóstico", "check"),
    )
    async def doctor_command(ctx: CommandContext) -> Reply:
        return Reply.paginated("\n".join(await doctor(ctx.services)))
