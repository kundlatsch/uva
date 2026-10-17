from __future__ import annotations

from uva.plugins.api import PluginContext, command

TEXTS = {
    "en": {
        "usage": "Usage: /dashboard",
        "nothing": "nothing",
        "no_key": "no key",
        "no_title": "no title",
        "no_date": "no date",
        "never": "never",
        "at": "at",
        "next": "next",
        "last": "last",
        "no_label": "no label",
        "no_status": "no status",
        "today": "today",
        "month": "month",
        "memory": "Working memory",
        "goals": "Active goals",
        "context": "Recent context",
        "expenses": "Expenses",
    },
    "pt": {
        "usage": "Uso: /painel",
        "nothing": "nada",
        "no_key": "sem chave",
        "no_title": "sem título",
        "no_date": "sem data",
        "never": "nunca",
        "at": "em",
        "next": "próxima",
        "last": "última",
        "no_label": "sem rótulo",
        "no_status": "sem status",
        "today": "hoje",
        "month": "mês",
        "memory": "Memória de trabalho",
        "goals": "Objetivos ativos",
        "context": "Contexto recente",
        "expenses": "Gastos",
    },
}


def texts_for(lang: str) -> dict[str, str]:
    return TEXTS.get(lang, TEXTS["en"])


def text_or(value: object, default: str) -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def section(title: str, entries: list[str], texts: dict[str, str] = TEXTS["pt"]) -> str:
    if not entries:
        return f"{title}\n{texts['nothing']}"
    return f"{title}\n" + "\n".join(entries)


def format_memory(item: dict[str, object], texts: dict[str, str] = TEXTS["pt"]) -> str:
    key = text_or(item.get("key"), texts["no_key"])
    value = text_or(item.get("value"), texts["nothing"])
    return f"- {key}: {value}"


def format_goal(item: dict[str, object], texts: dict[str, str] = TEXTS["pt"]) -> str:
    title = text_or(item.get("title"), text_or(item.get("id"), texts["no_title"]))
    next_run = text_or(item.get("next_run"), texts["no_date"])
    never = texts["never"]
    last_attempt = text_or(item.get("last_attempt"), never)
    last_attempt_at = text_or(item.get("last_attempt_at"), "")

    if last_attempt_at and last_attempt != never:
        last_attempt = f"{last_attempt} {texts['at']} {last_attempt_at}"
    elif last_attempt_at:
        last_attempt = last_attempt_at

    return f"- {title} ({texts['next']}: {next_run}; {texts['last']}: {last_attempt})"


def format_ref(item: dict[str, object], texts: dict[str, str] = TEXTS["pt"]) -> str:
    label = text_or(item.get("rotulo"), text_or(item.get("ref"), texts["no_label"]))
    status = text_or(item.get("status"), texts["no_status"])
    return f"- {label} — {status}"


@command(
    "painel",
    {
        "en": "Compact dashboard: memory, active goals, recent context and expenses",
        "pt": "Painel compacto: memória, objetivos ativos, contexto recente e gastos",
    },
    aliases=("dashboard", "panel"),
    usage="",
    examples=("ver painel",),
)
async def panel(ctx: PluginContext) -> str:
    texts = texts_for(ctx.lang)
    if ctx.args:
        return texts["usage"]

    memories = (await ctx.data.working_memory(limit=5))[:5]
    goals = await ctx.data.goals(status="ativo")
    refs = (await ctx.data.recent_refs(limit=5))[:5]
    today = await ctx.data.expenses_summary(period="hoje")
    month = await ctx.data.expenses_summary(period="mes")

    memory_lines = [format_memory(item, texts) for item in memories]
    goal_lines = [format_goal(item, texts) for item in goals]
    ref_lines = [format_ref(item, texts) for item in refs]

    today_total = text_or(today.get("total"), "R$ 0,00")
    month_total = text_or(month.get("total"), "R$ 0,00")
    expense_lines = [f"- {texts['today']}: {today_total}", f"- {texts['month']}: {month_total}"]

    return "\n\n".join(
        [
            section(texts["memory"], memory_lines, texts),
            section(texts["goals"], goal_lines, texts),
            section(texts["context"], ref_lines, texts),
            section(texts["expenses"], expense_lines, texts),
        ]
    )
