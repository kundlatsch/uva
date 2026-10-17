"""Plugin para rolar um dado com quantidade opcional de lados."""

import random

from uva.plugins.api import PluginContext, command


MIN_SIDES = 2
MAX_SIDES = 1000

USAGE = {
    "en": "Usage: /dice [sides], with sides between 2 and 1000.",
    "pt": "Uso: /dado [lados], com lados entre 2 e 1000.",
}


@command(
    "dado",
    {
        "en": "Rolls a die; 6 sides by default, or give the number of sides",
        "pt": "Rola um dado; por padrão de 6 lados, ou informe o número de lados",
    },
    aliases=("dice", "roll"),
    usage="[lados]",
    examples=("rola um d6", "rola um d20"),
)
def roll(ctx: PluginContext) -> str:
    usage = USAGE.get(ctx.lang, USAGE["en"])
    sides = 6
    if ctx.args:
        if len(ctx.args) != 1 or not ctx.args[0].isdigit():
            return usage
        sides = int(ctx.args[0])
        if not MIN_SIDES <= sides <= MAX_SIDES:
            return usage

    result = random.randint(1, sides)
    return f"🎲 {result} (d{sides})"
