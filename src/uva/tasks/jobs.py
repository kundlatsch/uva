"""Celery task definitions: thin wrappers around async functions."""

from __future__ import annotations

import random

from uva.actions.registry import RetryableActionError
from uva.tasks import runtime
from uva.tasks.app import app, settings
from uva.tasks.runner import record_heartbeat, run_action_task


def backoff_seconds(retries: int) -> float:
    """10s, 20s, 40s... capped at 5 minutes, with jitter."""
    return min(300, 10 * 2**retries) + random.uniform(0, 5)


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


@app.task(name="uva.heartbeat")
def heartbeat() -> None:
    async def job() -> None:
        await record_heartbeat(await runtime.get_services())

    runtime.run(job)
