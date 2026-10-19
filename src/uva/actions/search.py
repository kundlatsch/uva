"""Research: search a backend, summarize with the heavy model, list the sources."""

from __future__ import annotations

from dataclasses import dataclass, field

from uva.brain.vault import Note, Vault
from uva.i18n import t
from uva.llm.base import LLMError, LLMMessage
from uva.llm.budget import Scope
from uva.llm.prompts import summary_system_prompt
from uva.search import SearchError, SearchResult
from uva.logging.events import EventType, emit
from uva.safety import as_data, describe, sanitize_untrusted
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
            lines += ["", t("actions.search.sources")]
            lines += [f"- {s.title}: {s.url}" for s in self.sources if s.url]
        return "\n".join(lines)


def save_research(vault: Vault, found: Research, title: str | None = None) -> Note:
    """Store a research result as a reference note (03-recursos).

    ``title`` is the Portuguese title (the query may be in English)."""
    lines = [found.summary.strip()]
    if found.sources:
        lines += ["", "## Fontes", ""]
        lines += [f"- [{s.title or s.url}]({s.url})" for s in found.sources if s.url]
    note = vault.create_note(
        f"Pesquisa - {title or found.query}",
        "\n".join(lines),
        folder="recursos",
        tags=["pesquisa", found.backend],
    )
    vault.append_daily(f"Pesquisa: {note.link}")
    return note


class ResearchError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


async def research(services: Services, query: str, source: str, scope: Scope) -> Research:
    backend = services.search.get(source) or services.search.get("web")
    if backend is None:
        raise ResearchError(t("actions.search.no_backend"))

    try:
        response = await backend.search(query, services.settings.search_max_results)
    except SearchError as exc:
        raise ResearchError(t("actions.search.failed", error=exc), retryable=True) from exc

    if response.summary is not None:
        summary, findings = sanitize_untrusted(response.summary)
        if findings:
            await emit(EventType.ERROR, where="search_injection", query=query, findings=describe(findings))
    elif not response.results:
        summary = services.say("sem_resultado", termo=query)
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

    material, findings = [], []
    for i, r in enumerate(results, 1):
        snippet, found = sanitize_untrusted(r.snippet)
        findings += found
        material.append(f"[{i}] {r.title} ({r.url})\n{snippet}")
    if findings:
        await emit(EventType.ERROR, where="search_injection", query=query, findings=describe(findings))
    prompt = f"Question: {query}\n\n" + as_data("search results", "\n\n".join(material))
    try:
        response = await llm.complete(
            [LLMMessage("user", prompt)],
            purpose="search_summary",
            scope=scope,
            model=llm.heavy_model,
            system=summary_system_prompt(),
        )
    except LLMError as exc:
        raise ResearchError(t("actions.search.summarize_failed", error=exc), retryable=True) from exc
    return response.text
