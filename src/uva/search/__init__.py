"""Search backends and the factory that picks one from settings."""

from __future__ import annotations

from uva.config import LLMProvider, SearchBackendName, Settings
from uva.llm.client import LLMClient
from uva.search.base import (
    CompositeBackend,
    SearchBackend,
    SearchError,
    SearchResponse,
    SearchResult,
)
from uva.search.duckduckgo import DuckDuckGoBackend
from uva.search.wikipedia import WikipediaBackend


def build_search_backends(settings: Settings, llm: LLMClient | None) -> dict[str, SearchBackend]:
    """Backends by source name: "web" (default) and "wikipedia"."""
    wikipedia = WikipediaBackend(settings.wikipedia_lang)
    choice = settings.search_backend

    if choice is SearchBackendName.AUTO:
        use_anthropic = settings.llm_provider is LLMProvider.ANTHROPIC and llm is not None
        choice = (
            SearchBackendName.ANTHROPIC if use_anthropic else SearchBackendName.WIKIPEDIA_DUCKDUCKGO
        )

    web: SearchBackend
    match choice:
        case SearchBackendName.ANTHROPIC if llm is not None and llm.supports_web_search:
            from uva.search.anthropic_web import AnthropicWebSearchBackend

            web = AnthropicWebSearchBackend(llm)
        case SearchBackendName.WIKIPEDIA:
            web = wikipedia
        case SearchBackendName.DUCKDUCKGO:
            web = DuckDuckGoBackend()
        case _:
            web = CompositeBackend(wikipedia, DuckDuckGoBackend())

    return {"web": web, "wikipedia": wikipedia}


__all__ = [
    "CompositeBackend",
    "SearchBackend",
    "SearchError",
    "SearchResponse",
    "SearchResult",
    "build_search_backends",
]
