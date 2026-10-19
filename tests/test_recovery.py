import json
from datetime import UTC, datetime, timedelta

import pytest
from fakes import FakeTaskQueue, with_llm
from test_stm import InMemorySessionBackend

from uva.journal import DONE, FAILED, PROCESSING, InboundJournal, recovery_question
from uva.router import Router
from uva.stm.service import ShortTermMemory
from uva.storage.sqlite import Database
from uva.tasks.celery_queue import CeleryTaskQueue
from uva.tasks.queue import TaskStatus
from uva.tasks.runner import sweep_orphans

NOW = datetime(2026, 10, 13, 15, 0, tzinfo=UTC)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "uva.db")
    database.migrate()
    return database


async def test_journal_states_and_restart(db):
    journal = InboundJournal(db)
    done = await journal.received(1, "text", "oi", message_id=10, now=NOW - timedelta(minutes=5))
    await journal.mark(done, PROCESSING)
    await journal.mark(done, DONE)
    cut = await journal.received(1, "text", "pesquisa o preço do dólar", message_id=11, now=NOW - timedelta(minutes=3))
    await journal.mark(cut, PROCESSING)
    waiting = await journal.received(1, "voice", "[voice]", now=NOW - timedelta(minutes=2))
    old = await journal.received(1, "text", "coisa velha", now=NOW - timedelta(hours=9))
    asked = await journal.interrupted(NOW, max_hours=6)
    assert [e.id for e in asked] == [cut, waiting]  # old ones are not asked about
    for entry_id, state in ((done, DONE), (cut, FAILED), (waiting, FAILED), (old, FAILED)):
        assert (await journal.get(entry_id)).state == state
    assert await journal.interrupted(NOW) == []  # asked once only
    assert await journal.purge(NOW + timedelta(days=8)) == 4


async def test_recovery_question_and_buttons(services, db):
    services.sqlite = db
    services.stm = ShortTermMemory(InMemorySessionBackend(), services.settings)
    journal = InboundJournal(db)
    entry_id = await journal.received(1, "text", "qual a capital da Mongólia?", now=NOW)
    entry = (await journal.interrupted(NOW))[0]
    question = recovery_question(entry)
    assert "Fui desligada enquanto respondia “qual a capital da Mongólia?”" in question.text
    assert [b.data for b in question.buttons] == [f"rcvok:{entry_id}", f"rcvno:{entry_id}"]
    provider, _ = with_llm(services, json.dumps({"actions": [], "reply": "Ulan Bator."}))
    assert (await Router(services).handle_callback(f"rcvno:{entry_id}")).text == "Ok, deixei pra lá."
    assert provider.calls == []
    assert (await Router(services).handle_callback(f"rcvok:{entry_id}")).text == "Ulan Bator."
    assert provider.calls[0]["messages"][-1].content.endswith("qual a capital da Mongólia?")

    voice = await journal.received(1, "voice", "[voice]", now=NOW)
    entry = next(e for e in await journal.interrupted(NOW) if e.id == voice)
    media = recovery_question(entry)
    assert "áudio" in media.text and media.buttons == []


async def test_sweep_fails_orphans_but_not_idempotent_or_live_tasks(services):
    services.tasks = FakeTaskQueue()
    services.stm = ShortTermMemory(InMemorySessionBackend(), services.settings)
    store = services.tasks.store
    orphan = await services.tasks.enqueue("mcp_github_create_issue", {}, None)
    search = await services.tasks.enqueue("search", {"query": "x"}, None)
    live = await services.tasks.enqueue("mcp_github_comment", {}, None)
    queued = await services.tasks.enqueue("mcp_github_label", {}, None)
    for record in (orphan, search, live):
        await store.update(record.id, status=TaskStatus.RUNNING)
    await services.stm.add_ref("tarefa", orphan.id, "mcp: issue", "rodando")
    notices = []

    async def notify(reply):
        notices.append(reply.text)

    assert await sweep_orphans(services, {live.id}, notify) == [orphan.id]
    assert (await store.get(orphan.id)).status is TaskStatus.FAILED
    assert (await store.get(search.id)).status is TaskStatus.RUNNING  # given back by the broker
    assert (await store.get(live.id)).status is TaskStatus.RUNNING
    assert (await store.get(queued.id)).status is TaskStatus.QUEUED
    assert len(notices) == 1 and orphan.id in notices[0] and "interrompidas" in notices[0]
    session = await services.stm.session()
    assert session.find(orphan.id).status == "falhou"


def test_only_idempotent_tasks_are_acknowledged_late():
    from uva.tasks import jobs

    assert jobs.run_action_idempotent.acks_late and jobs.run_action_idempotent.reject_on_worker_lost
    assert not jobs.run_action.acks_late


async def test_queue_picks_the_task_by_action(monkeypatch):
    sent = []

    class FakeApp:
        def send_task(self, name, kwargs, task_id):
            sent.append(name)

    from fakes import InMemoryTaskStore

    queue = CeleryTaskQueue(InMemoryTaskStore(), redis=None)
    monkeypatch.setattr(CeleryTaskQueue, "_app", staticmethod(lambda: FakeApp()))
    await queue.enqueue("search", {"query": "x"}, None)
    await queue.enqueue("mcp_github_create_issue", {}, None)
    assert sent == ["uva.run_action_idempotent", "uva.run_action"]
