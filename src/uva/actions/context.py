"""task_status / retry / cancel: act on things the bot did earlier in the session."""

from __future__ import annotations

from pydantic import BaseModel, Field

from uva.actions.registry import ActionContext, ActionRegistry, ActionResult
from uva.i18n import t
from uva.logging.events import current_trace_id
from uva.stm.status import refresh

REF_HELP = "Referência do contexto recente (ex.: t3), id, parte do rótulo, ou vazio para a em foco"


class RefParams(BaseModel):
    ref: str | None = Field(default=None, description=REF_HELP)


async def _resolve(ctx: ActionContext, query: str | None):
    stm = ctx.services.stm
    if stm is None:
        return None, ActionResult(text=t("actions.context.no_stm"))
    session = await stm.session()
    ref = session.find(query)
    if ref is None:
        return None, ActionResult(
            text=t("actions.context.unknown_ref", query=query or t("actions.context.nothing_in_focus"))
        )
    return ref, None


def register(registry: ActionRegistry) -> None:
    @registry.action(
        "task_status",
        "Estado real de algo que eu disparei antes (tarefa, pesquisa, feature, objetivo)",
        RefParams,
    )
    async def task_status(params: RefParams, ctx: ActionContext) -> ActionResult:
        ref, error = await _resolve(ctx, params.ref)
        if error:
            return error
        ref, live = await refresh(ctx.services, ref)
        detail = f" ({live.detail})" if live.detail else ""
        return ActionResult(
            text=f"{ref.rotulo} [{ref.ref}]: {live.status}{detail}",
            data={"ref": ref.ref, "status": live.status},
        )

    @registry.action("retry", "Roda de novo uma tarefa/pesquisa/feature que falhou", RefParams)
    async def retry(params: RefParams, ctx: ActionContext) -> ActionResult:
        ref, error = await _resolve(ctx, params.ref)
        if error:
            return error
        ref, live = await refresh(ctx.services, ref)
        services = ctx.services
        if live.status != "falhou":
            return ActionResult(
                text=t("actions.context.retry.not_failed", label=ref.rotulo, status=live.status)
            )
        if ref.tipo in ("tarefa", "pesquisa") and services.tasks is not None:
            old = await services.tasks.store.get(ref.id)
            if old is None:
                return ActionResult(text=t("actions.context.retry.record_gone", label=ref.rotulo))
            new = await services.tasks.enqueue(old.action, old.params, current_trace_id())
            if services.stm is not None:
                await services.stm.add_ref(ref.tipo, new.id, ref.rotulo, "na fila")
            return ActionResult(text=t("actions.context.retry.rerunning", label=ref.rotulo, id=new.id))
        if ref.tipo == "feature" and services.features is not None:
            old_feature = await services.features.store.get(ref.id)
            if old_feature is None:
                return ActionResult(text=t("actions.context.retry.feature_gone"))
            from uva.commands.features import request_feature  # avoid an import cycle

            return ActionResult(text=await request_feature(services, old_feature.description))
        return ActionResult(text=t("actions.context.retry.unsupported", kind=ref.tipo))

    @registry.action("cancel", "Cancela uma tarefa/pesquisa que ainda está rodando", RefParams)
    async def cancel(params: RefParams, ctx: ActionContext) -> ActionResult:
        ref, error = await _resolve(ctx, params.ref)
        if error:
            return error
        services = ctx.services
        if ref.tipo == "projeto" and services.projects is not None:
            if await services.projects.cancel(ref.id) is None:
                return ActionResult(text=t("actions.context.cancel.already_done", label=ref.rotulo))
            return ActionResult(text=t("actions.context.cancel.stopping", label=ref.rotulo))
        if ref.tipo not in ("tarefa", "pesquisa") or services.tasks is None:
            return ActionResult(text=t("actions.context.cancel.unsupported", kind=ref.tipo))
        if not await services.tasks.cancel(ref.id):
            return ActionResult(text=t("actions.context.cancel.already_done", label=ref.rotulo))
        if services.stm is not None:
            await services.stm.update_ref(ref.id, "falhou", "cancelada")
        return ActionResult(
            text=t("actions.context.cancel.cancelled", phrase=services.say("cancelado", id=ref.id), label=ref.rotulo)
        )
