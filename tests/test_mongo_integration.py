"""Tests against a real MongoDB. Skipped unless MONGO_TEST_URI is set.

Run them on the compose network with scripts/run-integration-tests.sh.
"""

import os
from datetime import timedelta

import pytest

from uva.clock import utcnow
from uva.logging.events import Event, EventQuery, EventType
from uva.storage import mongo

MONGO_TEST_URI = os.environ.get("MONGO_TEST_URI")
pytestmark = pytest.mark.skipif(not MONGO_TEST_URI, reason="MONGO_TEST_URI not set")


@pytest.fixture
async def db():
    client = mongo.connect(MONGO_TEST_URI)
    database = mongo.get_database(client)
    await client.drop_database(database.name)
    yield database
    await client.drop_database(database.name)
    await client.close()


async def test_event_store_roundtrip(db):
    store = mongo.MongoEventStore(db, ttl_days=1)
    await store.setup()
    old = Event(trace_id="t1", type=EventType.ROUTE, ts=utcnow() - timedelta(hours=3))
    await store.add(old)
    await store.add(Event(trace_id="t2", type=EventType.LLM_CALL, data={"cost_usd": 0.01}))

    recent = await store.find(EventQuery(since=utcnow() - timedelta(hours=1)))
    assert [e.trace_id for e in recent] == ["t2"]
    assert recent[0].data == {"cost_usd": 0.01}

    llm = await store.find(EventQuery(types={EventType.LLM_CALL}))
    assert len(llm) == 1
    assert (await store.find(EventQuery(trace_id="t1")))[0].ts.tzinfo is not None


async def test_usage_store_sums_by_period_and_scope(db):
    from uva.llm.budget import Scope, UsageRecord

    store = mongo.MongoUsageStore(db)
    await store.setup()

    def record(cost, scope, hours_ago=0):
        return UsageRecord(
            ts=utcnow() - timedelta(hours=hours_ago), provider="p", model="m",
            input_tokens=1, output_tokens=1, cost_usd=cost, scope=scope, purpose="t",
        )

    await store.add(record(0.5, Scope.INTERACTIVE))
    await store.add(record(0.25, Scope.PROACTIVE))
    await store.add(record(9.0, Scope.INTERACTIVE, hours_ago=48))

    since = utcnow() - timedelta(hours=1)
    assert await store.total_cost(since) == 0.75
    assert await store.total_cost(since, Scope.PROACTIVE) == 0.25


async def test_task_store_roundtrip(db):
    from uva.tasks.queue import TaskRecord, TaskStatus

    store = mongo.MongoTaskStore(db, ttl_days=1)
    await store.setup()
    record = TaskRecord(action="search", params={"query": "x"})
    await store.add(record)
    await store.update(record.id, status=TaskStatus.DONE)
    loaded = await store.get(record.id)
    assert loaded.status is TaskStatus.DONE and loaded.params == {"query": "x"}
    assert [r.id for r in await store.recent(5)] == [record.id]


REDIS_TEST_URL = os.environ.get("REDIS_TEST_URL")


@pytest.mark.skipif(not REDIS_TEST_URL, reason="REDIS_TEST_URL not set")
async def test_busy_tracker_with_real_redis():
    from redis.asyncio import Redis

    from uva.tasks.busy import BusyTracker

    redis = Redis.from_url(REDIS_TEST_URL, decode_responses=True)
    await redis.flushdb()
    busy = BusyTracker(redis)
    assert not await busy.is_busy()
    await busy.mark("chat", 60)
    assert await busy.reasons() == ["chat"]
    await busy.clear("chat")
    assert not await busy.is_busy()
    await redis.aclose()
