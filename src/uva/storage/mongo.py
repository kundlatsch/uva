"""MongoDB connection and the Mongo-backed event store."""

from __future__ import annotations

from datetime import UTC
from typing import Any

from pymongo import ASCENDING, DESCENDING, AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase

from uva.logging.events import Event, EventQuery

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
