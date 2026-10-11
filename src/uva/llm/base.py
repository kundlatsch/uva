"""Provider-neutral types for talking to an LLM."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


@dataclass(frozen=True, slots=True)
class LLMMessage:
    role: Literal["user", "assistant"]
    content: str
    # Inline images as base64 data URLs ("data:image/jpeg;base64,...").
    images: tuple[str, ...] = ()


@dataclass(slots=True)
class LLMResponse:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    # Provider-specific extras, e.g. web search request counts or citations.
    extra: dict[str, Any] = field(default_factory=dict)


class LLMError(RuntimeError):
    """Any failure talking to the provider (network, auth, bad request...)."""


class LLMProvider(Protocol):
    name: str

    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        model: str,
        system: str | None = None,
        json_schema: dict[str, Any] | None = None,
        max_tokens: int = 4000,
    ) -> LLMResponse:
        """One completion. With ``json_schema`` the reply text must be JSON
        matching it (enforced natively when the provider supports it)."""
        ...
