"""/frases: the ready-made phrases (data/fallback_phrases.json)."""

from __future__ import annotations

from uva.commands.registry import CommandContext, CommandRegistry
from uva.i18n import t
from uva.phrases import placeholders

SAMPLES = 5


def register(registry: CommandRegistry) -> None:
    @registry.command(
        "frases",
        t("commands.phrases.frases.description"),
        aliases=("phrases",),
        usage="[categoria]",
    )
    async def phrases(ctx: CommandContext) -> str:
        assert ctx.services.phrases is not None
        categories = ctx.services.phrases.categories
        if not categories:
            return t("commands.phrases.frases.missing_file", path=ctx.services.settings.phrases_file)
        if not ctx.args:
            total = sum(len(v) for v in categories.values())
            lines = [t("commands.phrases.frases.header", total=total, count=len(categories))]
            lines += [f"- {name}: {len(items)}" for name, items in sorted(categories.items())]
            lines.append(t("commands.phrases.frases.examples_hint"))
            return "\n".join(lines)

        name = ctx.args[0].lower()
        if name not in categories:
            return t("commands.phrases.frases.unknown_category", name=name)
        samples = ctx.services.phrases.rng.sample(categories[name], min(SAMPLES, len(categories[name])))
        lines = [t("commands.phrases.frases.samples_header", name=name, count=len(categories[name]))]
        for phrase in samples:
            marks = placeholders(phrase)
            uses = t("commands.phrases.frases.uses", marks=", ".join(sorted(marks))) if marks else ""
            lines.append(f"- {phrase}{uses}")
        return "\n".join(lines)
