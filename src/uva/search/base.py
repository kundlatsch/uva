"""Pluggable search backends used by the ``search`` action."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True, slots=True)
class SearchResult:
    title: str
    url: str
    snippet: str = ""


@dataclass(slots=True)
class SearchResponse:
    results: list[SearchResult] = field(default_factory=list)
    # Some backends (Anthropic web search) already return a written answer.
    summary: str | None = None


class SearchError(RuntimeError):
    pass


class SearchBackend(Protocol):
    name: str

    async def search(self, query: str, max_results: int) -> SearchResponse: ...


class CompositeBackend:
    """Queries several backends and concatenates their results."""

    def __init__(self, *backends: SearchBackend) -> None:
        self.backends = backends
        self.name = "+".join(b.name for b in backends)

    async def search(self, query: str, max_results: int) -> SearchResponse:
        combined = SearchResponse()
        errors: list[str] = []
        for backend in self.backends:
            try:
                response = await backend.search(query, max_results)
            except SearchError as exc:
                errors.append(f"{backend.name}: {exc}")
                continue
            combined.results.extend(response.results)
        if not combined.results and errors:
            raise SearchError("; ".join(errors))
        return combined
