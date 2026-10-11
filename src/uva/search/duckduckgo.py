"""Free generic web search through the ``ddgs`` package (DuckDuckGo and others)."""

from __future__ import annotations

import asyncio

from uva.search.base import SearchError, SearchResponse, SearchResult


class DuckDuckGoBackend:
    name = "duckduckgo"

    def __init__(self, region: str = "br-pt") -> None:
        self.region = region

    async def search(self, query: str, max_results: int) -> SearchResponse:
        try:
            hits = await asyncio.to_thread(self._search_sync, query, max_results)
        except Exception as exc:  # ddgs raises its own exception types
            raise SearchError(f"duckduckgo: {exc}") from exc
        return SearchResponse(
            results=[
                SearchResult(
                    title=hit.get("title", ""),
                    url=hit.get("href", ""),
                    snippet=hit.get("body", ""),
                )
                for hit in hits
            ]
        )

    def _search_sync(self, query: str, max_results: int) -> list[dict]:
        from ddgs import DDGS  # imported lazily: heavy import, optional backend

        return DDGS().text(query, region=self.region, max_results=max_results)
