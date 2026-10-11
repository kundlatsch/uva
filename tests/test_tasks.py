import json

import pytest
from fakes import FakeBusy, FakeSearch, FakeTaskQueue, with_llm

from uva.actions.registry import RetryableActionError
from uva.logging.events import EventType
from uva.router import Router
from uva.search.base import SearchError, SearchResult
from uva.tasks.jobs import backoff_seconds
from uva.tasks.queue import TaskStatus
from uva.tasks.runner import run_action_task

SEARCH_PLAN = json.dumps(
    {"actions": [{"type": "search", "params": {"query": "F1"}}], "reply": "Vou pesquisar."}
)


@pytest.fixture
def queue(services) -> FakeTaskQueue:
    services.tasks = FakeTaskQueue()
    services.busy = FakeBusy()
    return services.tasks


class Notifications:
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def __call__(self, reply) -> None:
        self.texts.append(reply.text)


async def run(services, record, attempt=0, notify=None):
    return await run_action_task(
        services,
        notify or Notifications(),
        task_id=record.id,
        action_name=record.action,
        params=record.params,
        trace_id=record.trace_id,
        attempt=attempt,
        max_retries=3,
    )


async def test_search_is_enqueued_not_run_inline(services, queue, event_store):
    web = FakeSearch("web", SearchResult("F1", "https://f1.com", "texto"))
    services.search = {"web": web}
    with_llm(services, SEARCH_PLAN)

    reply = await Router(services).handle_text("pesquisa sobre F1")

    task = queue.sent[0]
    assert (task.action, task.params) == ("search", {"query": "F1", "source": "web"})
    assert "Pesquisando" in reply.text and f"/cancelar {task.id}" in reply.text
    assert web.queries == []  # the worker does it
    assert event_store.of_type(EventType.TASK_ENQUEUED)[0].data["task_id"] == task.id


async def test_ping_still_answers_while_tasks_are_queued(services, queue):
    with_llm(services, SEARCH_PLAN, SEARCH_PLAN)
    router = Router(services)
    await router.handle_text("pesquisa sobre F1")
    await router.handle_text("pesquisa sobre F1 de novo")
    assert len(queue.sent) == 2
    assert (await router.handle_text("/ping")).text == "pong"


async def test_messages_mark_the_bot_busy(services, queue):
    await Router(services).handle_text("/ping")
    assert "chat" in services.busy.markers


async def test_worker_runs_task_and_notifies(services, queue, event_store):
    services.search = {"web": FakeSearch("web", SearchResult("F1", "https://f1.com", "texto"))}
    with_llm(services, "Resumo da F1.")
    record = await queue.enqueue("search", {"query": "F1"}, "trace123")
    notify = Notifications()

    status = await run(services, record, notify=notify)

    assert status is TaskStatus.DONE
    assert "Resumo da F1." in notify.texts[0] and "https://f1.com" in notify.texts[0]
    assert queue.store.records[record.id].status is TaskStatus.DONE
    assert services.busy.history[-1] == f"task:{record.id}" and services.busy.markers == {}
    # Events from the worker join the trace of the original message.
    assert {e.trace_id for e in event_store.of_type(EventType.TASK_FINISHED)} == {"trace123"}


class FailingSearch:
    name = "web"

    async def search(self, query, max_results):
        raise SearchError("timeout")


async def test_transient_failure_retries_then_gives_up(services, queue):
    services.search = {"web": FailingSearch()}
    with_llm(services)
    record = await queue.enqueue("search", {"query": "F1"}, None)
    notify = Notifications()

    with pytest.raises(RetryableActionError):
        await run(services, record, attempt=0, notify=notify)
    assert queue.store.records[record.id].status is TaskStatus.RETRYING
    assert notify.texts == []

    # Last attempt: no retries left, so the user gets the failure message.
    assert await run(services, record, attempt=3, notify=notify) is TaskStatus.DONE
    assert "Não consegui pesquisar" in notify.texts[0]


async def test_cancelled_task_is_skipped(services, queue):
    record = await queue.enqueue("search", {"query": "F1"}, None)
    assert await queue.cancel(record.id)
    notify = Notifications()
    assert await run(services, record, notify=notify) is TaskStatus.CANCELLED
    assert notify.texts == []


async def test_tasks_and_cancel_commands(services, queue):
    record = await queue.enqueue("search", {"query": "F1"}, None)
    router = Router(services)
    listing = (await router.handle_text("/tarefas")).text
    assert record.id in listing and '"F1"' in listing and "na fila" in listing
    assert "cancelada" in (await router.handle_text(f"/cancelar {record.id}")).text
    assert "Não achei" in (await router.handle_text(f"/cancelar {record.id}")).text


def test_backoff_grows_and_is_capped():
    assert 10 <= backoff_seconds(0) < 16
    assert 40 <= backoff_seconds(2) < 46
    assert backoff_seconds(10) <= 305
