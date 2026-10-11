"""MongoDB connection and the Mongo-backed stores."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pymongo import ASCENDING, DESCENDING, AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase

from uva.clock import utcnow
from uva.llm.budget import Scope, UsageRecord
from uva.goals.store import Goal, GoalKind, GoalStatus, pick_due
from uva.logging.events import Event, EventQuery
from uva.memory.store import MemoryItem
from uva.tasks.queue import TaskRecord, TaskStatus

DEFAULT_DB_NAME = "uva"


def connect(uri: str) -> AsyncMongoClient:
    # tz_aware so datetimes come back as aware UTC values.
    return AsyncMongoClient(uri, tz_aware=True, tzinfo=UTC, serverSelectionTimeoutMS=3000)


def get_database(client: AsyncMongoClient) -> AsyncDatabase:
    try:
        return client.get_default_database()
    except Exception:  # URI without a database name
        return client[DEFAULT_DB_NAME]


async def ping(client: AsyncMongoClient) -> bool:
    try:
        await client.admin.command("ping")
        return True
    except Exception:
        return False


class MongoEventStore:
    def __init__(self, db: AsyncDatabase, ttl_days: int) -> None:
        self.collection = db["events"]
        self.ttl_days = ttl_days

    async def setup(self) -> None:
        await self.collection.create_index("ts", expireAfterSeconds=self.ttl_days * 86400)
        await self.collection.create_index([("trace_id", ASCENDING), ("ts", ASCENDING)])
        await self.collection.create_index([("type", ASCENDING), ("ts", ASCENDING)])

    async def add(self, event: Event) -> None:
        await self.collection.insert_one(event.model_dump(mode="python"))

    async def find(self, query: EventQuery) -> list[Event]:
        # Newest `limit` events, returned oldest first.
        cursor = (
            self.collection.find(_to_filter(query), {"_id": False})
            .sort("ts", DESCENDING)
            .limit(query.limit)
        )
        events = [Event.model_validate(doc) async for doc in cursor]
        events.reverse()
        return events


def _to_filter(query: EventQuery) -> dict[str, Any]:
    filt: dict[str, Any] = {}
    if query.trace_id:
        filt["trace_id"] = query.trace_id
    if query.types:
        filt["type"] = {"$in": [t.value for t in query.types]}
    ts: dict[str, Any] = {}
    if query.since:
        ts["$gte"] = query.since
    if query.until:
        ts["$lt"] = query.until
    if ts:
        filt["ts"] = ts
    return filt


class MongoUsageStore:
    """LLM calls and their cost (collection ``llm_usage``)."""

    def __init__(self, db: AsyncDatabase) -> None:
        self.collection = db["llm_usage"]

    async def setup(self) -> None:
        await self.collection.create_index([("ts", ASCENDING), ("scope", ASCENDING)])

    async def add(self, record: UsageRecord) -> None:
        doc = record.model_dump()
        doc["scope"] = record.scope.value
        await self.collection.insert_one(doc)

    async def total_cost(self, since: datetime, scope: Scope | None = None) -> float:
        match: dict[str, Any] = {"ts": {"$gte": since}}
        if scope is not None:
            match["scope"] = scope.value
        cursor = await self.collection.aggregate(
            [{"$match": match}, {"$group": {"_id": None, "total": {"$sum": "$cost_usd"}}}]
        )
        rows = await cursor.to_list(length=1)
        return float(rows[0]["total"]) if rows else 0.0


class MongoTaskStore:
    """Background task records (collection ``tasks``), kept for TASKS_TTL_DAYS."""

    def __init__(self, db: AsyncDatabase, ttl_days: int) -> None:
        self.collection = db["tasks"]
        self.ttl_days = ttl_days

    async def setup(self) -> None:
        await self.collection.create_index("id", unique=True)
        await self.collection.create_index("created_at", expireAfterSeconds=self.ttl_days * 86400)

    async def add(self, record: TaskRecord) -> None:
        doc = record.model_dump()
        doc["status"] = record.status.value
        await self.collection.insert_one(doc)

    async def get(self, task_id: str) -> TaskRecord | None:
        doc = await self.collection.find_one({"id": task_id}, {"_id": False})
        return TaskRecord.model_validate(doc) if doc else None

    async def update(self, task_id: str, **fields: Any) -> None:
        fields = {k: (v.value if isinstance(v, TaskStatus) else v) for k, v in fields.items()}
        fields["updated_at"] = utcnow()
        await self.collection.update_one({"id": task_id}, {"$set": fields})

    async def recent(self, limit: int) -> list[TaskRecord]:
        cursor = self.collection.find({}, {"_id": False}).sort("created_at", DESCENDING).limit(limit)
        return [TaskRecord.model_validate(doc) async for doc in cursor]


class MongoMemoryStore:
    """Collections ``memory`` (TTL on expires_at), ``memory_archive`` and ``memory_meta``."""

    WORKING_SET_ID = "working_set"

    def __init__(self, db: AsyncDatabase) -> None:
        self.collection = db["memory"]
        self.archive_collection = db["memory_archive"]
        self.meta = db["memory_meta"]

    async def setup(self) -> None:
        await self.collection.create_index("key", unique=True)
        # expireAfterSeconds=0: each document expires at its own expires_at.
        await self.collection.create_index("expires_at", expireAfterSeconds=0)

    async def get(self, key: str) -> MemoryItem | None:
        doc = await self.collection.find_one({"key": key}, {"_id": False})
        return MemoryItem.model_validate(doc) if doc else None

    async def upsert(self, item: MemoryItem) -> None:
        doc = item.model_dump()
        doc["category"] = item.category.value
        await self.collection.replace_one({"key": item.key}, doc, upsert=True)

    async def delete(self, key: str) -> bool:
        result = await self.collection.delete_one({"key": key})
        return result.deleted_count > 0

    async def all(self) -> list[MemoryItem]:
        return [MemoryItem.model_validate(d) async for d in self.collection.find({}, {"_id": False})]

    async def archive(self, items: list[MemoryItem], reason: str) -> None:
        now = utcnow()
        docs = [
            {**i.model_dump(), "category": i.category.value, "archived_at": now, "reason": reason}
            for i in items
        ]
        await self.archive_collection.insert_many(docs)
        await self.collection.delete_many({"key": {"$in": [i.key for i in items]}})

    async def load_working_set(self) -> tuple[list[str], datetime] | None:
        doc = await self.meta.find_one({"_id": self.WORKING_SET_ID})
        return (doc["keys"], doc["computed_at"]) if doc else None

    async def save_working_set(self, keys: list[str], computed_at: datetime) -> None:
        await self.meta.replace_one(
            {"_id": self.WORKING_SET_ID},
            {"keys": keys, "computed_at": computed_at},
            upsert=True,
        )


class MongoGoalStore:
    """Collection ``goals``."""

    def __init__(self, db: AsyncDatabase) -> None:
        self.collection = db["goals"]

    async def setup(self) -> None:
        await self.collection.create_index("id", unique=True)
        await self.collection.create_index([("status", ASCENDING), ("next_run_at", ASCENDING)])

    @staticmethod
    def _doc(goal: Goal) -> dict[str, Any]:
        doc = goal.model_dump()
        doc["kind"], doc["status"] = goal.kind.value, goal.status.value
        return doc

    async def add(self, goal: Goal) -> None:
        await self.collection.insert_one(self._doc(goal))

    async def get(self, goal_id: str) -> Goal | None:
        doc = await self.collection.find_one({"id": goal_id}, {"_id": False})
        return Goal.model_validate(doc) if doc else None

    async def update(self, goal_id: str, **fields: Any) -> None:
        clean: dict[str, Any] = {}
        for key, value in fields.items():
            if isinstance(value, GoalStatus | GoalKind):
                value = value.value
            elif key == "results":
                value = [r.model_dump() for r in value]
            clean[key] = value
        await self.collection.update_one({"id": goal_id}, {"$set": clean})

    async def delete(self, goal_id: str) -> bool:
        return (await self.collection.delete_one({"id": goal_id})).deleted_count > 0

    async def list(self, status: GoalStatus | None = None) -> list[Goal]:
        query = {"status": status.value} if status else {}
        cursor = self.collection.find(query, {"_id": False}).sort("created_at", ASCENDING)
        return [Goal.model_validate(d) async for d in cursor]

    async def due(self, now: datetime) -> list[Goal]:
        cursor = self.collection.find(
            {"status": GoalStatus.ATIVO.value, "next_run_at": {"$lte": now}}, {"_id": False}
        )
        return pick_due([Goal.model_validate(d) async for d in cursor], now)
