"""Celery task definitions: thin wrappers around async functions.

Every task tells the owner on Telegram how it ended (PROACTIVE_NOTIFY);
memory maintenance only speaks up when it fails.
"""

from __future__ import annotations

import random

from uva.actions.registry import RetryableActionError
from uva.goals import runner as goals_runner
from uva.logging import get_logger
from uva.logging.events import EventType, emit, trace
from uva.replies import Reply
from uva.tasks import runtime
from uva.tasks.app import app, settings
from uva.tasks.runner import record_heartbeat, run_action_task

log = get_logger(__name__)


def backoff_seconds(retries: int) -> float:
    """10s, 20s, 40s... capped at 5 minutes, with jitter."""
    return min(300, 10 * 2**retries) + random.uniform(0, 5)


def _notify_failure(what: str, exc: BaseException) -> None:
    """Best effort: a broken Telegram must not hide the original error."""
    try:
        runtime.run(lambda: runtime.notify(Reply(f"⚠️ {what} falhou: {exc}")))
    except Exception as notify_exc:
        log.warning("notify_failed", error=str(notify_exc))


@app.task(bind=True, name="uva.run_action", max_retries=settings.task_max_retries)
def run_action(self, task_id: str, action: str, params: dict, trace_id: str | None) -> str:
    async def job() -> str:
        services = await runtime.get_services()
        status = await run_action_task(
            services,
            runtime.notify,
            task_id=task_id,
            action_name=action,
            params=params,
            trace_id=trace_id,
            attempt=self.request.retries,
            max_retries=self.max_retries,
        )
        return status.value

    try:
        return runtime.run(job)
    except RetryableActionError as exc:
        raise self.retry(exc=exc, countdown=backoff_seconds(self.request.retries)) from exc
    except Exception as exc:
        _notify_failure(f"A tarefa {task_id} ({action})", exc)
        raise


@app.task(name="uva.memory_maintenance")
def memory_maintenance() -> dict:
    async def job() -> dict:
        services = await runtime.get_services()
        with trace():
            result = await services.memory.maintenance()  # type: ignore[union-attr]
            await emit(EventType.MEMORY, op="maintenance", **result)
        return result

    try:
        return runtime.run(job)
    except Exception as exc:
        _notify_failure("A limpeza diária da memória", exc)
        raise


@app.task(name="uva.proactive_tick")
def proactive_tick() -> str:
    async def job() -> str:
        notify = runtime.notify if settings.proactive_notify else None
        return await goals_runner.proactive_tick(await runtime.get_services(), notify=notify)

    try:
        return runtime.run(job)
    except Exception as exc:
        if settings.proactive_notify:
            _notify_failure("O loop proativo", exc)
        raise


@app.task(name="uva.heartbeat")
def heartbeat() -> None:
    async def job() -> None:
        await record_heartbeat(await runtime.get_services())

    runtime.run(job)
