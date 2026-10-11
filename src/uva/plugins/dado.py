"""Plugin para rolar um dado com quantidade opcional de lados."""

import random

from uva.plugins.api import PluginContext, command


MIN_SIDES = 2
MAX_SIDES = 1000


@command(
    "dado",
    "Rola um dado; por padrão de 6 lados, ou informe o número de lados",
    usage="[lados]",
    examples=("rola um d6", "rola um d20"),
)
def roll(ctx: PluginContext) -> str:
    sides = 6
    if ctx.args:
        if len(ctx.args) != 1 or not ctx.args[0].isdigit():
            return "Uso: /dado [lados], com lados entre 2 e 1000."
        sides = int(ctx.args[0])
        if not MIN_SIDES <= sides <= MAX_SIDES:
            return "Uso: /dado [lados], com lados entre 2 e 1000."

    result = random.randint(1, sides)
    return f"🎲 {result} (d{sides})"
