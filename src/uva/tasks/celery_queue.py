"""TaskQueue backed by Celery (Redis broker) with records in Mongo."""

from __future__ import annotations

import asyncio
from typing import Any

from redis.asyncio import Redis

from uva.tasks.queue import IDEMPOTENT_ACTIONS, TaskRecord, TaskStatus, TaskStore

# Celery's default queue is a Redis list with this name.
CELERY_QUEUE_KEY = "celery"


class CeleryTaskQueue:
    def __init__(self, store: TaskStore, redis: Redis) -> None:
        self.store = store
        self.redis = redis

    @staticmethod
    def _app():
        # Imported lazily so the bot only loads Celery when it enqueues.
        from uva.tasks.app import app

        return app

    async def enqueue(self, action: str, params: dict[str, Any], trace_id: str | None) -> TaskRecord:
        record = TaskRecord(action=action, params=params, trace_id=trace_id)
        await self.store.add(record)
        kwargs = {"task_id": record.id, "action": action, "params": params, "trace_id": trace_id}
        # Idempotent work (a search) is acknowledged only when done, so a worker that
        # dies mid-task gets it again; the rest must not run twice.
        name = "uva.run_action_idempotent" if action in IDEMPOTENT_ACTIONS else "uva.run_action"
        # send_task is blocking network I/O; keep it off the event loop.
        await asyncio.to_thread(self._app().send_task, name, kwargs=kwargs, task_id=record.id)
        return record

    async def cancel(self, task_id: str) -> bool:
        record = await self.store.get(task_id)
        if record is None or record.status.finished:
            return False
        await asyncio.to_thread(self._app().control.revoke, task_id, terminate=True)
        await self.store.update(task_id, status=TaskStatus.CANCELLED)
        return True

    async def queue_size(self) -> int | None:
        return int(await self.redis.llen(CELERY_QUEUE_KEY))
