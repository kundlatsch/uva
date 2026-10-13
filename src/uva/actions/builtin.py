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
from uva.actions.search import ResearchError, research, save_research


class ReplyParams(BaseModel):
    text: str = Field(description="Mensagem para o usuário")


class RunCommandParams(BaseModel):
    command: str = Field(description="Nome do comando, sem a barra (ex.: log)")
    args: list[str] = Field(default_factory=list, description="Argumentos (ex.: ['2h', 'erros'])")


class SearchParams(BaseModel):
    model_config = ConfigDict(json_schema_extra=require_title_pt)

    query: str = Field(description="O que pesquisar, em poucas palavras")
    title_pt: str = Field(
        default="",
        description=(
            "REQUIRED. Short title in Brazilian Portuguese for the research note (user content; "
            "becomes its file name), e.g. 'Previsão do tempo em Mafra'. Never English."
        ),
    )
    source: Literal["web", "wikipedia"] = Field(
        default="web", description="'wikipedia' quando o usuário pedir Wikipédia"
    )


def register(registry: ActionRegistry) -> None:
    @registry.action("reply", "Envia uma mensagem ao usuário", ReplyParams, proactive_safe=True)
    async def reply(params: ReplyParams, ctx: ActionContext) -> ActionResult:
        return ActionResult(text=params.text)

    @registry.action("run_command", "Executa um comando do bot", RunCommandParams)
    async def run_command(params: RunCommandParams, ctx: ActionContext) -> ActionResult:
        if ctx.run_command is None:
            return ActionResult(text="Não posso executar comandos agora.")
        command = params.command.lstrip("/")
        if ctx.services.registry.get(command) is None:
            return ActionResult(text=f"Comando desconhecido: /{command}")
        result = await ctx.run_command(command, params.args)
        return ActionResult(text=result.text, data={"command": command, "args": params.args})

    @registry.action(
        "search",
        "Pesquisa um assunto (web ou Wikipédia) e devolve um resumo com fontes",
        SearchParams,
        proactive_safe=True,
        long_running=True,
        progress_text="🔎 Pesquisando…",
        progress_phrase="pesquisando",
    )
    async def search(params: SearchParams, ctx: ActionContext) -> ActionResult:
        try:
            found = await research(ctx.services, params.query, params.source, ctx.scope)
        except ResearchError as exc:
            if exc.retryable and ctx.allow_retry:
                raise RetryableActionError(str(exc)) from exc
            return ActionResult(text=f"Não consegui pesquisar: {exc}", data={"error": str(exc)})
        text = found.render()
        note_path = None
        if ctx.services.vault is not None:
            title = await portuguese_title(
                ctx.services, params.title_pt, found.summary, scope=ctx.scope, alternatives=(found.query,)
            )
            note_path = save_research(ctx.services.vault, found, title).path
            text += f"\n\n📝 Salvo em {note_path}"
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
