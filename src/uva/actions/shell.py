"""shell_run / python_run: the LLM can use the bot's own folder /uva."""

from __future__ import annotations

from pydantic import BaseModel, Field

from uva.actions.registry import ActionContext, ActionRegistry, ActionResult
from uva.i18n import t


class ShellRunParams(BaseModel):
    command: str = Field(min_length=1, description="Bash command, runs in /uva")


class PythonRunParams(BaseModel):
    code: str = Field(min_length=1, description="Python 3 code, runs in /uva (packages: pip in the venv)")


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
        "Runs a Linux command in your own folder /uva (isolated; deleting/overwriting asks "
        "the user for confirmation)",
        ShellRunParams,
    )
    async def shell_run(params: ShellRunParams, ctx: ActionContext) -> ActionResult:
        return await run("sh", params.command, ctx)

    @registry.action(
        "python_run",
        "Runs Python code in your folder /uva (its own venv; install packages with shell_run pip)",
        PythonRunParams,
    )
    async def python_run(params: PythonRunParams, ctx: ActionContext) -> ActionResult:
        return await run("py", params.code, ctx)
