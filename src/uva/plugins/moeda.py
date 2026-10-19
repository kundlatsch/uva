"""Example plugin: flip a coin. Copy this shape for new commands."""

import random

from uva.plugins.api import PluginContext, command

# Every text the user reads, per language (ctx.lang is "en" or "pt").
TEXTS = {
    "en": {
        "usage": "Usage: /coin [times], with times between 1 and 20.",
        "faces": ("heads", "tails"),
        "count": "{heads} heads, {tails} tails",
    },
    "pt": {
        "usage": "Uso: /moeda [vezes], com vezes entre 1 e 20.",
        "faces": ("cara", "coroa"),
        "count": "{heads} cara, {tails} coroa",
    },
}


@command(
    "moeda",
    {"en": "Flips a coin: heads or tails", "pt": "Joga uma moeda: cara ou coroa"},
    aliases=("coin",),
    usage={"en": "[times]", "pt": "[vezes]"},
    examples=("joga uma moeda", "cara ou coroa"),
)
def flip(ctx: PluginContext) -> str:
    texts = TEXTS.get(ctx.lang, TEXTS["en"])
    times = 1
    if ctx.args:
        if not ctx.args[0].isdigit() or not 1 <= int(ctx.args[0]) <= 20:
            return texts["usage"]
        times = int(ctx.args[0])
    heads, tails = texts["faces"]
    results = [random.choice([heads, tails]) for _ in range(times)]
    if times == 1:
        return f"🪙 {results[0].capitalize()}!"
    count = texts["count"].format(heads=results.count(heads), tails=results.count(tails))
    return f"🪙 {', '.join(results)} ({count})"
