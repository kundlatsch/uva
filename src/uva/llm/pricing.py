"""Cost estimates per model, in USD per 1M tokens (input, output).

Built-in values cover Anthropic models. Anything else (e.g. OpenAI models)
should be set via LLM_PRICES in the .env; unknown models use the default
price, which is deliberately pessimistic so budgets err on the safe side.
"""

from __future__ import annotations

Price = tuple[float, float]

DEFAULT_PRICES: dict[str, Price] = {
    "claude-fable-5-1": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-haiku-4-5": (1.0, 5.0),
}

# Anthropic web search: $10 per 1000 searches.
WEB_SEARCH_COST_USD = 0.01


# OpenAI bills prompt-cache hits at ~10% of the input price (GPT-5 family).
CACHED_INPUT_FACTOR = 0.1

class PriceTable:
    def __init__(self, overrides: dict[str, Price], default: Price) -> None:
        self.prices = {**DEFAULT_PRICES, **overrides}
        self.default = default

    def lookup(self, model: str) -> Price | None:
        """Exact match, else the longest configured name the model starts with
        (APIs often answer with dated names like "gpt-5.5-2026-04-23")."""
        if model in self.prices:
            return self.prices[model]
        prefixes = [name for name in self.prices if model.startswith(name)]
        return self.prices[max(prefixes, key=len)] if prefixes else None

    def knows(self, model: str) -> bool:
        return self.lookup(model) is not None

    def cost(self, model: str, input_tokens: int, output_tokens: int, cached_input_tokens: int = 0) -> float:
        """Cached input (prompt cache hits) is billed at CACHED_INPUT_FACTOR of the price."""
        price_in, price_out = self.lookup(model) or self.default
        cached = min(cached_input_tokens, input_tokens)
        billed_in = (input_tokens - cached) + cached * CACHED_INPUT_FACTOR
        return (billed_in * price_in + output_tokens * price_out) / 1_000_000
