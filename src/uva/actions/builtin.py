"""Initial actions: reply, run_command and search."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from uva.actions.registry import (
    ActionContext,
    ActionRegistry,
    ActionResult,
    RetryableActionError,
)
from uva.brain.naming import portuguese_title, require_title_pt
from uva.i18n import t
from uva.actions.search import ResearchError, research, save_research


class ReplyParams(BaseModel):
    text: str = Field(description="Message for the user")


class RunCommandParams(BaseModel):
    command: str = Field(description="Command name, without the slash (e.g. log)")
    args: list[str] = Field(default_factory=list, description="Arguments (e.g. ['2h', 'errors'])")


class SearchParams(BaseModel):
    model_config = ConfigDict(json_schema_extra=require_title_pt)

    query: str = Field(description="What to search for, in a few words")
    title_pt: str = Field(
        default="",
        description=(
            "REQUIRED. Short title in Brazilian Portuguese for the research note (user content; "
            "becomes its file name), e.g. 'Previsão do tempo em Mafra'. Never English."
        ),
    )
    source: Literal["web", "wikipedia"] = Field(
        default="web", description="'wikipedia' when the user asks for Wikipedia"
    )


def register(registry: ActionRegistry) -> None:
    @registry.action("reply", "Sends a message to the user", ReplyParams, proactive_safe=True)
    async def reply(params: ReplyParams, ctx: ActionContext) -> ActionResult:
        return ActionResult(text=params.text)

    @registry.action("run_command", "Runs a bot command", RunCommandParams)
    async def run_command(params: RunCommandParams, ctx: ActionContext) -> ActionResult:
        if ctx.run_command is None:
            return ActionResult(text=t("actions.builtin.run_command.unavailable"))
        command = params.command.lstrip("/")
        if ctx.services.registry.get(command) is None:
            return ActionResult(text=t("actions.builtin.run_command.unknown", command=command))
        result = await ctx.run_command(command, params.args)
        return ActionResult(text=result.text, data={"command": command, "args": params.args})

    @registry.action(
        "search",
        "Researches a topic (web or Wikipedia) and returns a summary with sources",
        SearchParams,
        proactive_safe=True,
        long_running=True,
        progress_text=t("actions.builtin.search.progress"),
        progress_phrase="pesquisando",
    )
    async def search(params: SearchParams, ctx: ActionContext) -> ActionResult:
        try:
            found = await research(ctx.services, params.query, params.source, ctx.scope)
        except ResearchError as exc:
            if exc.retryable and ctx.allow_retry:
                raise RetryableActionError(str(exc)) from exc
            return ActionResult(text=t("actions.builtin.search.failed", error=exc), data={"error": str(exc)})
        text = found.render()
        note_path = None
        if ctx.services.vault is not None:
            title = await portuguese_title(
                ctx.services, params.title_pt, found.summary, scope=ctx.scope, alternatives=(found.query,)
            )
            note_path = save_research(ctx.services.vault, found, title).path
            text += "\n\n" + t("actions.builtin.search.saved", path=note_path)
        return ActionResult(
            text=text,
            data={
                "note": note_path,
                "query": found.query,
                "backend": found.backend,
                "summary": found.summary,
                "sources": [{"title": s.title, "url": s.url} for s in found.sources],
            },
        )
