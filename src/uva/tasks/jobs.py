"""Celery task definitions: thin wrappers around async functions.

Every task tells the owner on Telegram how it ended (PROACTIVE_NOTIFY);
memory maintenance only speaks up when it fails.
"""

from __future__ import annotations

import random

from uva.actions.registry import RetryableActionError
from uva import initiatives
from uva.goals import runner as goals_runner
from uva.i18n import t
from uva.index.service import notice_source, turn_source
from uva.logging import get_logger
from uva.logging.events import EventType, emit, trace
from uva.phrases import Phrases
from uva.replies import Reply
from uva.selfmod.reload import request_reload
from uva.projects.runner import build_project
from uva.selfmod.selfcode import build_feature
from uva.stm.promote import promote_idle_sessions
from uva.tasks import runtime
from uva.tasks.app import app, settings
from uva.tasks.runner import record_heartbeat, run_action_task

log = get_logger(__name__)


def backoff_seconds(retries: int) -> float:
    """10s, 20s, 40s... capped at 5 minutes, with jitter."""
    return min(300, 10 * 2**retries) + random.uniform(0, 5)


def _notify_failure(what: str, exc: BaseException, task_id: str | None = None) -> None:
    """Best effort: a broken Telegram must not hide the original error."""
    phrase = Phrases(settings.phrases_file).pick("tarefa_falhou", **({"id": task_id} if task_id else {}))
    try:
        text = t("tasks.jobs.failed", phrase=phrase, what=what, error=exc)
        runtime.run(lambda: runtime.notify(Reply(text)))
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
        _notify_failure(t("tasks.jobs.what.task", id=task_id, action=action), exc, task_id)
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
        _notify_failure(t("tasks.jobs.what.memory"), exc)
        raise


@app.task(name="uva.proactive_tick")
def proactive_tick() -> str:
    async def job() -> str:
        notice_source.set("goal")
        notify = runtime.notify if settings.proactive_notify else None
        return await goals_runner.proactive_tick(await runtime.get_services(), notify=notify)

    try:
        return runtime.run(job)
    except Exception as exc:
        if settings.proactive_notify:
            _notify_failure(t("tasks.jobs.what.proactive"), exc)
        raise


@app.task(
    name="uva.selfcode",
    time_limit=settings.selfcode_time_limit_seconds,
    soft_time_limit=settings.selfcode_time_limit_seconds - 30,
)
def selfcode(feature_id: str) -> str:
    async def job() -> str:
        services = await runtime.get_services()
        redis = services.busy.redis  # type: ignore[union-attr]
        record = await build_feature(
            services, feature_id, runtime.notify, request_reload=lambda: request_reload(redis)
        )
        return record.status.value

    try:
        return runtime.run(job)
    except Exception as exc:
        _notify_failure(t("tasks.jobs.what.selfcode", id=feature_id), exc)
        raise


@app.task(
    name="uva.project_build",
    time_limit=(settings.project_timeout_minutes + 5) * 60,
    soft_time_limit=(settings.project_timeout_minutes + 4) * 60,
)
def project_build(project_id: str) -> str:
    async def job() -> str:
        record = await build_project(await runtime.get_services(), project_id, runtime.notify)
        return record.status.value if record else "missing"

    try:
        return runtime.run(job)
    except Exception as exc:
        _notify_failure(t("tasks.jobs.what.project", id=project_id), exc)
        raise


@app.task(name="uva.routines_tick", time_limit=600, soft_time_limit=570)
def routines_tick() -> list[str]:
    from uva.routines import run_due

    async def job() -> list[str]:
        notice_source.set("routine")
        turn_source.set("routine")
        return await run_due(await runtime.get_services(), runtime.notify)

    try:
        return runtime.run(job)
    except Exception as exc:
        _notify_failure(t("tasks.jobs.what.routines"), exc)
        raise


@app.task(name="uva.stm_promotion")
def stm_promotion() -> dict:
    async def job() -> dict:
        return await promote_idle_sessions(await runtime.get_services())

    return runtime.run(job)


@app.task(name="uva.initiative_tick")
def initiative_tick() -> str:
    async def job() -> str:
        notice_source.set("initiative")
        services = await runtime.get_services()
        status, message = await initiatives.initiative_tick(services)
        if message:
            await runtime.notify(Reply(message))
        return status

    return runtime.run(job)


@app.task(name="uva.review_tick")
def review_tick() -> str:
    """Review recent conversation into memory, skills and profile when due."""
    from uva.review import review_tick as tick

    async def job() -> str:
        return await tick(await runtime.get_services(), runtime.notify)

    return runtime.run(job)


@app.task(name="uva.journal_digest")
def journal_digest() -> str:
    """Yesterday's conversation summarized in yesterday's journal note."""
    from uva.review import journal_digest as digest

    async def job() -> str:
        return await digest(await runtime.get_services())

    return runtime.run(job)


@app.task(name="uva.index_sync")
def index_sync() -> dict:
    """Pick up notes edited outside UVA (Obsidian) and drop deleted ones."""

    async def job() -> dict:
        services = await runtime.get_services()
        if services.index is None:
            return {}
        return await services.index.sync_notes()

    return runtime.run(job)


@app.task(name="uva.heartbeat")
def heartbeat() -> None:
    async def job() -> None:
        await record_heartbeat(await runtime.get_services())

    runtime.run(job)
