"""Redis storage for short-term sessions, with optimistic locking (WATCH)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

from redis.asyncio import Redis
from redis.exceptions import WatchError

from uva.stm.session import Session

PREFIX = "stm:"


class RedisSessionBackend:
    def __init__(self, redis: Redis) -> None:
        self.redis = redis

    async def load(self, user_id: int) -> Session | None:
        raw = await self.redis.get(f"{PREFIX}{user_id}")
        return Session.model_validate_json(raw) if raw else None

    async def update(self, user_id: int, change: Callable[[Session], None], ttl: timedelta) -> Session:
        key = f"{PREFIX}{user_id}"
        # Bot and worker may write at the same time: retry on conflict.
        for _ in range(20):
            async with self.redis.pipeline() as pipe:
                try:
                    await pipe.watch(key)
                    raw = await pipe.get(key)
                    session = Session.model_validate_json(raw) if raw else Session()
                    change(session)
                    pipe.multi()
                    pipe.set(key, session.model_dump_json())
                    if session.has_active_refs:
                        pipe.persist(key)  # never lose a task that is still running
                    else:
                        pipe.expire(key, int(ttl.total_seconds()))
                    await pipe.execute()
                    return session
                except WatchError:
                    continue
        raise RuntimeError("could not save the short-term memory (write conflict)")

    async def delete(self, user_id: int) -> None:
        await self.redis.delete(f"{PREFIX}{user_id}")

    async def users(self) -> list[int]:
        return [int(key.removeprefix(PREFIX)) async for key in self.redis.scan_iter(f"{PREFIX}*")]
