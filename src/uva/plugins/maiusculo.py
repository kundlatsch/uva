"""Plugin para converter texto em letras maiúsculas."""

from uva.plugins.api import PluginContext, command

USAGE = {
    "en": "Usage: /uppercase <text>",
    "pt": "Uso: /maiusculo <texto>",
}


@command(
    "maiusculo",
    {"en": "Converts a text to UPPERCASE LETTERS", "pt": "Converte um texto para LETRAS MAIÚSCULAS"},
    aliases=("maiúsculo", "uppercase", "upper"),
    usage={"en": "<text>", "pt": "<texto>"},
    examples=("bom dia", "Rua 7, apto 12!"),
)
def uppercase(ctx: PluginContext) -> str:
    if not ctx.text:
        return USAGE.get(ctx.lang, USAGE["en"])
    return ctx.text.upper()
