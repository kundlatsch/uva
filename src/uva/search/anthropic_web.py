"""Claude's own web search tool, used when the provider is Anthropic."""

from __future__ import annotations

from uva.llm.base import LLMError
from uva.llm.budget import Scope
from uva.llm.client import LLMClient
from uva.search.base import SearchError, SearchResponse, SearchResult

SYSTEM = (
    "Você pesquisa na web para um assistente pessoal. Responda em português do Brasil "
    "com um resumo objetivo do que encontrou, citando os fatos principais."
)


class AnthropicWebSearchBackend:
    name = "anthropic"

    def __init__(self, llm: LLMClient, scope: Scope = Scope.INTERACTIVE) -> None:
        self.llm = llm
        self.scope = scope

    async def search(self, query: str, max_results: int) -> SearchResponse:
        try:
            response = await self.llm.web_search(query, system=SYSTEM, scope=self.scope)
        except LLMError as exc:
            raise SearchError(str(exc)) from exc
        sources = response.extra.get("sources", [])[:max_results]
        return SearchResponse(
            results=[SearchResult(title=s["title"], url=s["url"]) for s in sources],
            summary=response.text,
        )
