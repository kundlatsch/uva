"""Real status of a reference (from Mongo/Celery records), plus retry/cancel."""

from __future__ import annotations

from dataclasses import dataclass

from uva.i18n import t
from uva.services import Services
from uva.stm.service import status_label
from uva.stm.session import Ref, RefStatus
from uva.tasks.queue import TaskStatus

_TASK_STATUS: dict[TaskStatus, RefStatus] = {
    TaskStatus.QUEUED: "na fila",
    TaskStatus.RUNNING: "rodando",
    TaskStatus.RETRYING: "rodando",
    TaskStatus.DONE: "ok",
    TaskStatus.FAILED: "falhou",
    TaskStatus.CANCELLED: "falhou",
}


@dataclass(frozen=True, slots=True)
class LiveStatus:
    status: RefStatus
    detail: str  # plain text shown to the user, e.g. "cancelada" or the error


async def live_status(services: Services, ref: Ref) -> LiveStatus:
    """Ask the source of truth; falls back to what the session knows."""
    if ref.tipo in ("tarefa", "pesquisa") and services.tasks is not None:
        record = await services.tasks.store.get(ref.id)
        if record is not None:
            cancelled = record.status is TaskStatus.CANCELLED
            detail = t("stm.status.cancelled") if cancelled else (record.error or "")
            return LiveStatus(_TASK_STATUS[record.status], detail)
    if ref.tipo == "feature" and services.features is not None:
        record = await services.features.store.get(ref.id)
        if record is not None:
            status: RefStatus = {
                "queued": "na fila", "running": "rodando", "merged": "ok",
            }.get(record.status.value, "falhou")  # type: ignore[assignment]
            detail = f"/{record.command}" if record.command and status == "ok" else (record.error or "")
            return LiveStatus(status, detail)
    if ref.tipo == "objetivo" and services.goals is not None:
        goal = await services.goals.get(ref.id)
        if goal is not None:
            last = goal.results[-1].summary if goal.results else t("stm.status.never_ran")
            return LiveStatus("ok", t("stm.status.goal", status=goal.status.value, last=last[:150]))
    return LiveStatus(ref.status, ref.erro or "")


async def refresh(services: Services, ref: Ref) -> tuple[Ref, LiveStatus]:
    """Live status, written back into the session (so the prompt stays right)."""
    live = await live_status(services, ref)
    if services.stm is not None and (live.status != ref.status):
        updated = await services.stm.update_ref(ref.id, live.status, live.detail or None)
        ref = updated or ref
    elif services.stm is not None:
        await services.stm.set_focus(ref.ref)
    return ref, live


def status_reply(services: Services, ref: Ref, live: LiveStatus) -> str:
    """Answer to "como tá aquela tarefa?" without the LLM."""
    subject = f"{ref.rotulo} ({ref.ref}, id {ref.id})"
    if live.status == "ok":
        tail = f": {live.detail}" if live.detail else "."
        return f"{services.say('tarefa_ok', id=ref.id)}\n" + t("stm.status.done", subject=subject, tail=tail)
    if live.status == "falhou":
        why = f": {live.detail}" if live.detail else "."
        phrase = services.say("tarefa_falhou", id=ref.id)
        return f"{phrase}\n" + t("stm.status.failed", subject=subject, why=why)
    status = status_label(live.status)
    return f"{services.say('ocupado')}\n" + t("stm.status.in_progress", subject=subject, status=status)
