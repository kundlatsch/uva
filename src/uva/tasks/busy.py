"""The "busy" lock in Redis.

The bot marks itself busy while the user is chatting and while tasks run;
the proactive goal loop skips its turn whenever anything is marked.
Each marker is its own key with a TTL, so a crashed task can't leave the
bot busy forever.
"""

from __future__ import annotations

from redis.asyncio import Redis

PREFIX = "uva:busy:"
# Written by the heartbeat task (beat -> worker); read by /status.
HEARTBEAT_KEY = "uva:heartbeat"


class BusyTracker:
    def __init__(self, redis: Redis) -> None:
        self.redis = redis

    async def mark(self, reason: str, ttl_seconds: int) -> None:
        await self.redis.set(PREFIX + reason, "1", ex=ttl_seconds)

    async def clear(self, reason: str) -> None:
        await self.redis.delete(PREFIX + reason)

    async def reasons(self) -> list[str]:
        return [key.removeprefix(PREFIX) async for key in self.redis.scan_iter(PREFIX + "*")]

    async def is_busy(self) -> bool:
        return bool(await self.reasons())
