"""Research: search a backend, summarize with the heavy model, list the sources."""

from __future__ import annotations

from dataclasses import dataclass, field

from uva.brain.vault import Note, Vault
from uva.llm.base import LLMError, LLMMessage
from uva.llm.budget import Scope
from uva.llm.prompts import summary_system_prompt
from uva.search import SearchError, SearchResult
from uva.services import Services


@dataclass(slots=True)
class Research:
    query: str
    source: str
    backend: str
    summary: str
    sources: list[SearchResult] = field(default_factory=list)

    def render(self) -> str:
        lines = [f"🔎 {self.query}", "", self.summary.strip()]
        if self.sources:
            lines += ["", "Fontes:"]
            lines += [f"- {s.title}: {s.url}" for s in self.sources if s.url]
        return "\n".join(lines)


def save_research(vault: Vault, found: Research) -> Note:
    """Store a research result as a reference note (03-recursos)."""
    lines = [found.summary.strip()]
    if found.sources:
        lines += ["", "## Fontes", ""]
        lines += [f"- [{s.title or s.url}]({s.url})" for s in found.sources if s.url]
    note = vault.create_note(
        f"Pesquisa - {found.query}",
        "\n".join(lines),
        folder="recursos",
        tags=["pesquisa", found.backend],
    )
    vault.append_daily(f"Pesquisa: [[{note.title}]]")
    return note


class ResearchError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


async def research(services: Services, query: str, source: str, scope: Scope) -> Research:
    backend = services.search.get(source) or services.search.get("web")
    if backend is None:
        raise ResearchError("nenhum backend de pesquisa configurado")

    try:
        response = await backend.search(query, services.settings.search_max_results)
    except SearchError as exc:
        raise ResearchError(f"a pesquisa falhou ({exc})", retryable=True) from exc

    if response.summary is not None:
        summary = response.summary
    elif not response.results:
        summary = "Não encontrei resultados para essa pesquisa."
    else:
        summary = await _summarize(services, query, response.results, scope)

    return Research(query, source, backend.name, summary, response.results)


async def _summarize(
    services: Services, query: str, results: list[SearchResult], scope: Scope
) -> str:
    llm = services.llm
    if llm is None:
        # No LLM: fall back to the raw snippets.
        return "\n\n".join(f"{r.title}: {r.snippet[:400]}" for r in results)

    material = "\n\n".join(
        f"[{i}] {r.title} ({r.url})\n{r.snippet}" for i, r in enumerate(results, 1)
    )
    prompt = f"Pergunta: {query}\n\nResultados:\n{material}"
    try:
        response = await llm.complete(
            [LLMMessage("user", prompt)],
            purpose="search_summary",
            scope=scope,
            model=llm.heavy_model,
            system=summary_system_prompt(),
        )
    except LLMError as exc:
        raise ResearchError(f"não consegui resumir os resultados ({exc})", retryable=True) from exc
    return response.text
