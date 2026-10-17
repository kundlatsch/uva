"""Plugin para converter texto em letras maiúsculas."""

from uva.plugins.api import PluginContext, command


@command(
    "maiusculo",
    "Converte um texto para LETRAS MAIÚSCULAS",
    aliases=("maiúsculo", "uppercase", "upper"),
    usage="<texto>",
    examples=("bom dia", "Rua 7, apto 12!"),
)
def uppercase(ctx: PluginContext) -> str:
    if not ctx.text:
        return "Uso: /maiusculo <texto>"
    return ctx.text.upper()
