"""What a background task actually does, independent of Celery (easy to test)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from uva.actions.registry import ActionContext, InvalidActionError, RetryableActionError
from uva.clock import utcnow
from uva.llm.agent import execute_action
from uva.llm.budget import Scope
from uva.logging.events import EventType, emit, trace
from uva.replies import Reply
from uva.services import Services
from uva.tasks.busy import HEARTBEAT_KEY
from uva.tasks.queue import TaskStatus

Notifier = Callable[[Reply], Awaitable[None]]


async def run_action_task(
    services: Services,
    notify: Notifier,
    *,
    task_id: str,
    action_name: str,
    params: dict,
    trace_id: str | None,
    attempt: int,
    max_retries: int,
) -> TaskStatus:
    """Run one queued action and send its result to the user.

    Raises RetryableActionError when the task should be retried.
    """
    assert services.tasks is not None and services.actions is not None
    store = services.tasks.store

    with trace(trace_id):
        record = await store.get(task_id)
        if record is not None and record.status is TaskStatus.CANCELLED:
            await emit(EventType.TASK_FINISHED, task_id=task_id, action=action_name, status="cancelled")
            return TaskStatus.CANCELLED

        await store.update(task_id, status=TaskStatus.RUNNING, attempts=attempt + 1)
        await _ref_status(services, task_id, "rodando", focus=False)
        busy_key = f"task:{task_id}"
        if services.busy is not None:
            await services.busy.mark(busy_key, services.settings.task_time_limit_seconds)
        try:
            try:
                action, validated = services.actions.validate(action_name, params)
            except InvalidActionError as exc:
                await _finish(services, task_id, action_name, TaskStatus.FAILED, str(exc))
                await _ref_status(services, task_id, "falhou", str(exc))
                phrase = services.say("tarefa_falhou", id=task_id)
                await notify(Reply(f"⚠️ {phrase}\n({action_name}: {exc})"))
                return TaskStatus.FAILED

            ctx = ActionContext(services, Scope.INTERACTIVE, allow_retry=attempt < max_retries)
            try:
                result = await execute_action(action, validated, ctx)
            except RetryableActionError as exc:
                await store.update(task_id, status=TaskStatus.RETRYING, error=str(exc))
                await emit(
                    EventType.TASK_FINISHED,
                    task_id=task_id,
                    action=action_name,
                    status="retrying",
                    attempt=attempt + 1,
                )
                raise

            # Record the final state first: the user may ask about it as soon as
            # the notification arrives.
            failed = bool(result.data.get("error"))
            await _finish(services, task_id, action_name, TaskStatus.DONE)
            await _ref_status(services, task_id, "falhou" if failed else "ok", result.data.get("error"))
            await notify(Reply(result.text or services.say("tarefa_ok", id=task_id)))
            return TaskStatus.DONE
        finally:
            if services.busy is not None:
                await services.busy.clear(busy_key)


async def _ref_status(services: Services, task_id: str, status, error=None, *, focus=True) -> None:
    """Keep the short-term memory in sync with the task (best effort)."""
    if services.stm is None:
        return
    try:
        await services.stm.update_ref(task_id, status, error, focus=focus)
    except Exception:
        pass


async def _finish(
    services: Services, task_id: str, action: str, status: TaskStatus, error: str | None = None
) -> None:
    assert services.tasks is not None
    await services.tasks.store.update(task_id, status=status, error=error)
    await emit(EventType.TASK_FINISHED, task_id=task_id, action=action, status=status.value, error=error)


async def record_heartbeat(services: Services) -> None:
    """Proves beat (scheduler) and worker are alive; read by /status."""
    if services.busy is not None:
        await services.busy.redis.set(HEARTBEAT_KEY, utcnow().isoformat(), ex=600)
