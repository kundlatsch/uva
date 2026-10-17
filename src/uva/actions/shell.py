"""shell_run / python_run: the LLM can use the bot's own folder /uva."""

from __future__ import annotations

from pydantic import BaseModel, Field

from uva.actions.registry import ActionContext, ActionRegistry, ActionResult
from uva.i18n import t


class ShellRunParams(BaseModel):
    command: str = Field(min_length=1, description="Comando bash, roda em /uva")


class PythonRunParams(BaseModel):
    code: str = Field(min_length=1, description="Código Python 3, roda em /uva (pacotes: pip no venv)")


def register(registry: ActionRegistry) -> None:
    async def run(kind: str, code: str, ctx: ActionContext) -> ActionResult:
        shell = ctx.services.shell
        if shell is None:
            return ActionResult(text=t("actions.shell.not_configured"))
        reply = await shell.request(kind, code)
        text = reply.pages and "\n\n".join(reply.pages) or reply.text
        return ActionResult(text=text, buttons=list(reply.buttons))

    @registry.action(
        "shell_run",
        "Roda um comando Linux na sua pasta própria /uva (isolada; apagar/sobrescrever pede "
        "confirmação ao usuário)",
        ShellRunParams,
    )
    async def shell_run(params: ShellRunParams, ctx: ActionContext) -> ActionResult:
        return await run("sh", params.command, ctx)

    @registry.action(
        "python_run",
        "Roda código Python na sua pasta /uva (venv próprio; instale pacotes com shell_run pip)",
        PythonRunParams,
    )
    async def python_run(params: PythonRunParams, ctx: ActionContext) -> ActionResult:
        return await run("py", params.code, ctx)
