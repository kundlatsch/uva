"""Asking for, listing and cancelling projects (the bot side)."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from datetime import tzinfo
from pathlib import Path

from uva.clock import format_local, start_of_day, utcnow
from uva.config import Settings
from uva.projects.store import ACTIVE, STATUS_LABEL, ProjectRecord, ProjectStatus, ProjectStore
from uva.text import normalize

Enqueue = Callable[[str], Awaitable[None]]

FILLER = frozenset(
    "um uma uns umas o a os as de do da dos das para pra pro com que e em no na cria crie criar "
    "faz faca fazer monta monte montar projeto novo nova me quero".split()
)
SLUG_WORDS = 5
SLUG_CHARS = 40


def slugify(description: str) -> str:
    """'cria um site de receitas com Flask' -> 'site-receitas-flask'."""
    words = [w for w in normalize(description).split() if w not in FILLER]
    slug = "-".join(words[:SLUG_WORDS])[:SLUG_CHARS].strip("-")
    return re.sub(r"-+", "-", slug) or "projeto"


class ProjectError(Exception):
    """A request that can't be accepted (shown to the user as is)."""


class ProjectService:
    def __init__(self, store: ProjectStore, settings: Settings, enqueue: Enqueue | None) -> None:
        self.store = store
        self.settings = settings
        self.enqueue = enqueue

    @property
    def root(self) -> Path:
        return Path(self.settings.projects_path)

    def configured(self) -> str | None:
        """Why projects can't run, or None."""
        s = self.settings
        if not s.projects_enabled:
            return "Projetos estão desligados (PROJECTS_ENABLED=false)."
        if self.enqueue is None:
            return "A fila de tarefas não está configurada."
        if s.claude_code_oauth_token is None and s.projects_anthropic_api_key is None:
            return (
                "Falta a credencial do Claude Code no .env: CLAUDE_CODE_OAUTH_TOKEN (gere com "
                "`claude setup-token`) ou PROJECTS_ANTHROPIC_API_KEY."
            )
        return None

    def _free_slug(self, base: str, taken: set[str]) -> str:
        slug, n = base, 2
        while slug in taken or (self.root / slug).exists():
            slug, n = f"{base}-{n}", n + 1
        return slug

    async def request(self, description: str, trace_id: str | None, tz: tzinfo) -> ProjectRecord:
        if (problem := self.configured()) is not None:
            raise ProjectError(problem)
        description = description.strip()
        if len(description) < 10:
            raise ProjectError("Descreva o projeto, ex.: /projeto um site estático de receitas com busca")
        recent = await self.store.recent(50)
        if running := [r for r in recent if r.status in ACTIVE]:
            raise ProjectError(f"Já tem um projeto em construção ({running[0].id}); veja /projetos.")
        spent = await self.store.cost_since(start_of_day(tz, utcnow()))
        if spent >= self.settings.project_daily_budget_usd:
            raise ProjectError(
                f"Orçamento diário de projetos esgotado (US$ {spent:.2f} de "
                f"{self.settings.project_daily_budget_usd:.2f}, PROJECT_DAILY_BUDGET_USD)."
            )
        slug = self._free_slug(slugify(description), {r.slug for r in recent})
        record = ProjectRecord(description=description, slug=slug, trace_id=trace_id)
        await self.store.add(record)
        assert self.enqueue is not None
        await self.enqueue(record.id)
        return record

    async def cancel(self, project_id: str) -> ProjectRecord | None:
        """Ask a queued/running project to stop. None if there is nothing to cancel."""
        record = await self.store.get(project_id)
        if record is None or record.status not in ACTIVE:
            return None
        if record.status is ProjectStatus.QUEUED:
            # Not started: the runner sees this and doesn't start.
            await self.store.update(project_id, status=ProjectStatus.CANCELLED, finished_at=utcnow())
        else:
            await self.store.update(project_id, status=ProjectStatus.CANCELLING)
        return record

    def host_path(self, slug: str) -> str:
        return f"{self.settings.projects_host_path.rstrip('/')}/{slug}"


def describe_project(record: ProjectRecord, tz: tzinfo, host_path: str) -> str:
    lines = [f"{record.id} {STATUS_LABEL[record.status]} {record.slug}", f"   {record.description[:120]}"]
    details = [format_local(record.created_at, tz, "%d/%m %H:%M")]
    if record.cost_usd:
        details.append(f"US$ {record.cost_usd:.2f}")
    if record.commits:
        details.append(f"{record.commits} commit(s)")
    if record.status is ProjectStatus.DONE:
        details.append(host_path)
    lines.append("   " + " · ".join(details))
    if record.error and record.status is not ProjectStatus.DONE:
        lines.append(f"   motivo: {record.error[:200]}")
    return "\n".join(lines)
