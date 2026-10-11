from __future__ import annotations

from uva.plugins.api import PluginContext, command


def text_or(value: object, default: str) -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def section(title: str, entries: list[str]) -> str:
    if not entries:
        return f"{title}\nnada"
    return f"{title}\n" + "\n".join(entries)


def format_memory(item: dict[str, object]) -> str:
    key = text_or(item.get("key"), "sem chave")
    value = text_or(item.get("value"), "nada")
    return f"- {key}: {value}"


def format_goal(item: dict[str, object]) -> str:
    title = text_or(item.get("title"), text_or(item.get("id"), "sem título"))
    next_run = text_or(item.get("next_run"), "sem data")
    last_attempt = text_or(item.get("last_attempt"), "nunca")
    last_attempt_at = text_or(item.get("last_attempt_at"), "")

    if last_attempt_at and last_attempt != "nunca":
        last_attempt = f"{last_attempt} em {last_attempt_at}"
    elif last_attempt_at:
        last_attempt = last_attempt_at

    return f"- {title} (próxima: {next_run}; última: {last_attempt})"


def format_ref(item: dict[str, object]) -> str:
    label = text_or(item.get("rotulo"), text_or(item.get("ref"), "sem rótulo"))
    status = text_or(item.get("status"), "sem status")
    return f"- {label} — {status}"


@command(
    "painel",
    "Painel compacto: memória, objetivos ativos, contexto recente e gastos",
    usage="",
    examples=("ver painel",),
)
async def panel(ctx: PluginContext) -> str:
    if ctx.args:
        return "Uso: /painel"

    memories = (await ctx.data.working_memory(limit=5))[:5]
    goals = await ctx.data.goals(status="ativo")
    refs = (await ctx.data.recent_refs(limit=5))[:5]
    today = await ctx.data.expenses_summary(period="hoje")
    month = await ctx.data.expenses_summary(period="mes")

    memory_lines = [format_memory(item) for item in memories]
    goal_lines = [format_goal(item) for item in goals]
    ref_lines = [format_ref(item) for item in refs]

    today_total = text_or(today.get("total"), "R$ 0,00")
    month_total = text_or(month.get("total"), "R$ 0,00")
    expense_lines = [f"- hoje: {today_total}", f"- mês: {month_total}"]

    return "\n\n".join(
        [
            section("Memória de trabalho", memory_lines),
            section("Objetivos ativos", goal_lines),
            section("Contexto recente", ref_lines),
            section("Gastos", expense_lines),
        ]
    )
