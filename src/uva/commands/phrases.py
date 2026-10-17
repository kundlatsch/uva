"""/frases: the ready-made phrases (data/fallback_phrases.json)."""

from __future__ import annotations

from uva.commands.registry import CommandContext, CommandRegistry
from uva.phrases import placeholders

SAMPLES = 5


def register(registry: CommandRegistry) -> None:
    @registry.command(
        "frases",
        "Categorias de frases prontas, ou exemplos de uma categoria",
        aliases=("phrases",),
        usage="[categoria]",
    )
    async def phrases(ctx: CommandContext) -> str:
        assert ctx.services.phrases is not None
        categories = ctx.services.phrases.categories
        if not categories:
            return f"Arquivo de frases ausente ou inválido ({ctx.services.settings.phrases_file})."
        if not ctx.args:
            total = sum(len(v) for v in categories.values())
            lines = [f"Frases prontas: {total} em {len(categories)} categorias"]
            lines += [f"- {name}: {len(items)}" for name, items in sorted(categories.items())]
            lines.append("Exemplos: /frases <categoria>")
            return "\n".join(lines)

        name = ctx.args[0].lower()
        if name not in categories:
            return f"Categoria desconhecida: {name}. Veja /frases."
        samples = ctx.services.phrases.rng.sample(categories[name], min(SAMPLES, len(categories[name])))
        lines = [f"{name} ({len(categories[name])} frases), alguns exemplos:"]
        for phrase in samples:
            marks = placeholders(phrase)
            lines.append(f"- {phrase}" + (f"  [usa: {', '.join(sorted(marks))}]" if marks else ""))
        return "\n".join(lines)
