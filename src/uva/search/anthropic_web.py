"""Claude's own web search tool, used when the provider is Anthropic."""

from __future__ import annotations

from uva.i18n import language_name
from uva.llm.base import LLMError
from uva.llm.budget import Scope
from uva.llm.client import LLMClient
from uva.search.base import SearchError, SearchResponse, SearchResult

def system_prompt() -> str:
    return (
        f"You search the web for a personal assistant. Answer in {language_name()} with an "
        "objective summary of what you found, citing the main facts."
    )


class AnthropicWebSearchBackend:
    name = "anthropic"

    def __init__(self, llm: LLMClient, scope: Scope = Scope.INTERACTIVE) -> None:
        self.llm = llm
        self.scope = scope

    async def search(self, query: str, max_results: int) -> SearchResponse:
        try:
            response = await self.llm.web_search(query, system=system_prompt(), scope=self.scope)
        except LLMError as exc:
            raise SearchError(str(exc)) from exc
        sources = response.extra.get("sources", [])[:max_results]
        return SearchResponse(
            results=[SearchResult(title=s["title"], url=s["url"]) for s in sources],
            summary=response.text,
        )
