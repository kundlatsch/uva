"""Example plugin: flip a coin. Copy this shape for new commands."""

import random

from uva.plugins.api import PluginContext, command


@command(
    "moeda",
    "Joga uma moeda: cara ou coroa",
    aliases=("coin",),
    usage="[vezes]",
    examples=("joga uma moeda", "cara ou coroa"),
)
def flip(ctx: PluginContext) -> str:
    times = 1
    if ctx.args:
        if not ctx.args[0].isdigit() or not 1 <= int(ctx.args[0]) <= 20:
            return "Uso: /moeda [vezes], com vezes entre 1 e 20."
        times = int(ctx.args[0])
    results = [random.choice(["cara", "coroa"]) for _ in range(times)]
    if times == 1:
        return f"🪙 {results[0].capitalize()}!"
    return f"🪙 {', '.join(results)} ({results.count('cara')} cara, {results.count('coroa')} coroa)"
