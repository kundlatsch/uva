"""Wikipedia via the MediaWiki API (search + plain-text intros)."""

from __future__ import annotations

import httpx

from uva.search.base import SearchError, SearchResponse, SearchResult

# Wikimedia asks API clients to identify themselves.
USER_AGENT = "UVA-Bot/0.1 (personal assistant; individual use)"
MAX_EXTRACT_CHARS = 1500


class WikipediaBackend:
    name = "wikipedia"

    def __init__(self, lang: str = "pt", client: httpx.AsyncClient | None = None) -> None:
        self.api_url = f"https://{lang}.wikipedia.org/w/api.php"
        self.client = client or httpx.AsyncClient(
            timeout=15, headers={"User-Agent": USER_AGENT}
        )

    async def search(self, query: str, max_results: int) -> SearchResponse:
        try:
            found = await self._get(
                {"action": "query", "list": "search", "srsearch": query, "srlimit": max_results}
            )
            page_ids = [str(hit["pageid"]) for hit in found.get("query", {}).get("search", [])]
            if not page_ids:
                return SearchResponse()
            pages = await self._get(
                {
                    "action": "query",
                    "prop": "extracts|info",
                    "exintro": 1,
                    "explaintext": 1,
                    "inprop": "url",
                    "pageids": "|".join(page_ids),
                }
            )
        except (httpx.HTTPError, ValueError) as exc:
            raise SearchError(f"wikipedia: {exc}") from exc

        by_id = pages.get("query", {}).get("pages", {})
        results = []
        for page_id in page_ids:  # keep search ranking order
            page = by_id.get(page_id)
            if not page:
                continue
            extract = (page.get("extract") or "").strip()
            results.append(
                SearchResult(
                    title=page.get("title", ""),
                    url=page.get("fullurl", ""),
                    snippet=extract[:MAX_EXTRACT_CHARS],
                )
            )
        return SearchResponse(results=results)

    async def _get(self, params: dict) -> dict:
        response = await self.client.get(
            self.api_url, params={**params, "format": "json", "formatversion": 1}
        )
        response.raise_for_status()
        return response.json()
