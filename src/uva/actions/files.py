"""file_read / file_write / file_append: the bot editing its own allowed files."""

from __future__ import annotations

from pydantic import BaseModel, Field

from uva.actions.registry import ActionContext, ActionRegistry, ActionResult
from uva.logging.events import EventType, emit
from uva.selfmod.files import FileAccessError

READ_LIMIT = 4000
PATH_HELP = (
    "Caminho relativo: data/soul.md, data/fallback_phrases.json, data/<arquivo>.md "
    "ou brain/<pasta>/<nota>.md"
)


class FileReadParams(BaseModel):
    path: str = Field(description=PATH_HELP)


class FileWriteParams(BaseModel):
    path: str = Field(description=PATH_HELP)
    content: str = Field(description="Conteúdo completo do arquivo")
    reason: str = Field(default="", description="Por que a mudança (vai na mensagem do commit)")


class FileAppendParams(BaseModel):
    path: str = Field(description=PATH_HELP)
    content: str = Field(description="Texto a acrescentar no fim")
    reason: str = Field(default="", description="Por que a mudança")


NOT_CONFIGURED = ActionResult(text="Edição de arquivos não está configurada.")


def register(registry: ActionRegistry) -> None:
    @registry.action(
        "file_read",
        "Lê um arquivo meu (soul.md, data/ ou second brain)",
        FileReadParams,
    )
    async def file_read(params: FileReadParams, ctx: ActionContext) -> ActionResult:
        files = ctx.services.files
        if files is None:
            return NOT_CONFIGURED
        try:
            text = files.read(params.path)
        except (FileAccessError, FileNotFoundError) as exc:
            return ActionResult(text=f"Não consegui ler {params.path}: {exc}", data={"ok": False})
        shown = text if len(text) <= READ_LIMIT else text[:READ_LIMIT] + "…"
        return ActionResult(text=f"{params.path}:\n\n{shown}", data={"ok": True, "chars": len(text)})

    @registry.action(
        "file_write",
        "Reescreve um arquivo meu em data/ (ex.: soul.md) ou cria uma nota nova no second brain",
        FileWriteParams,
    )
    async def file_write(params: FileWriteParams, ctx: ActionContext) -> ActionResult:
        return await _change(ctx, "write", params.path, params.content, params.reason)

    @registry.action(
        "file_append",
        "Acrescenta texto no fim de um arquivo meu (data/ ou second brain)",
        FileAppendParams,
    )
    async def file_append(params: FileAppendParams, ctx: ActionContext) -> ActionResult:
        return await _change(ctx, "append", params.path, params.content, params.reason)


async def _change(ctx: ActionContext, op: str, path: str, content: str, reason: str) -> ActionResult:
    files = ctx.services.files
    if files is None:
        return NOT_CONFIGURED
    try:
        change = await (files.write if op == "write" else files.append)(path, content, reason)
    except (FileAccessError, OSError) as exc:
        await emit(EventType.SELF_EDIT, op=op, path=path, ok=False, error=str(exc))
        return ActionResult(text=f"Não mexi em {path}: {exc}", data={"ok": False})
    await emit(EventType.SELF_EDIT, op=op, path=change.target.display, ok=True, commit=change.commit)
    commit = f" (commit {change.commit[:7]}; /reverter desfaz)" if change.commit else ""
    return ActionResult(
        text=f"✏️ {change.target.display} atualizado{commit}.",
        data={"ok": True, "commit": change.commit},
    )
