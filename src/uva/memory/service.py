"""Long-term memory: what the bot knows about the user.

* The LLM saves/updates/forgets items through actions; the user can too
  (/lembrar, /esquecer).
* The "working memory" is the top N items by score (relevance x recency x
  frequency). It is computed at startup, once a day (beat) and whenever
  memory changes, and goes into the system prompt.
* Daily maintenance archives items with a low score that have not been
  used for a long time. Expired items are removed by Mongo's TTL index.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Protocol

from uva.clock import utcnow
from uva.config import Settings
from uva.logging import get_logger
from uva.memory.store import MemoryCategory, MemoryItem, MemoryStore, normalize_key, score

if TYPE_CHECKING:
    from uva.index.service import SearchIndex

WORKING_SET_MAX_AGE = timedelta(hours=24)
VERSION_KEY = "memory:working_version"

log = get_logger(__name__)


class WorkingVersion(Protocol):
    """A counter shared by every process (Redis): bumped when the working set
    changes, so the bot drops its cached set after the worker saved a fact."""

    async def get(self) -> int: ...

    async def bump(self) -> int: ...


class RedisWorkingVersion:
    def __init__(self, redis, key: str = VERSION_KEY) -> None:
        self.redis = redis
        self.key = key

    async def get(self) -> int:
        return int(await self.redis.get(self.key) or 0)

    async def bump(self) -> int:
        return int(await self.redis.incr(self.key))


class MemoryHistory(Protocol):
    """Old values, kept before every change (Mongo ``memory_history``)."""

    async def add(self, entry: dict) -> None: ...

    async def for_key(self, key: str, limit: int = 20) -> list[dict]: ...


class MemoryService:
    def __init__(
        self,
        store: MemoryStore,
        settings: Settings,
        *,
        version: WorkingVersion | None = None,
        index: SearchIndex | None = None,
        history: MemoryHistory | None = None,
    ) -> None:
        self.store = store
        self.settings = settings
        self.version = version
        self.index = index
        self.history = history
        # (items, computed_at, version seen when they were loaded)
        self._cache: tuple[list[MemoryItem], datetime, int | None] | None = None

    # --- changes -------------------------------------------------------------

    async def save(
        self,
        key: str,
        value: str,
        *,
        category: MemoryCategory = MemoryCategory.FATO,
        relevance: float = 0.5,
        ttl_days: int | None = None,
        source: str | None = None,
        origin: str = "owner",
    ) -> MemoryItem:
        """Create or replace the value of ``key`` (keeps its history counters)."""
        now = utcnow()
        key = normalize_key(key)
        existing = await self.store.get(key)
        if existing is not None and (existing.value != value.strip() or existing.category != category):
            await self._superseded(existing, "replace")
        item = MemoryItem(
            key=key,
            value=value.strip(),
            category=category,
            relevance=relevance,
            created_at=existing.created_at if existing else now,
            last_used_at=now,
            use_count=(existing.use_count + 1) if existing else 0,
            expires_at=now + timedelta(days=ttl_days) if ttl_days else None,
            source=source,
            origin=origin,
        )
        await self.store.upsert(item)
        await self._indexed(item)
        await self.recompute_working_set(now)
        return item

    async def update(
        self,
        key: str,
        *,
        value: str | None = None,
        relevance: float | None = None,
        category: MemoryCategory | None = None,
        origin: str | None = None,
    ) -> MemoryItem | None:
        """Change fields of an existing item; counts as a use."""
        item = await self.store.get(normalize_key(key))
        if item is None:
            return None
        if (value is not None and value.strip() != item.value) or (category is not None and category != item.category):
            await self._superseded(item, "update")
        changes: dict = {"last_used_at": utcnow(), "use_count": item.use_count + 1}
        if value is not None:
            changes["value"] = value.strip()
        if relevance is not None:
            changes["relevance"] = relevance
        if category is not None:
            changes["category"] = category
        if origin is not None:
            changes["origin"] = origin
        item = item.model_copy(update=changes)
        await self.store.upsert(item)
        await self._indexed(item)
        await self.recompute_working_set()
        return item

    async def forget(self, key: str) -> bool:
        key = normalize_key(key)
        if (existing := await self.store.get(key)) is not None:
            await self._superseded(existing, "forget")
        removed = await self.store.delete(key)
        if removed:
            if self.index is not None:
                try:
                    await self.index.remove_memory(key)
                except Exception as exc:
                    log.warning("index_memory_failed", key=key, error=str(exc))
            await self.recompute_working_set()
        return removed

    async def _superseded(self, item: MemoryItem, reason: str) -> None:
        """Keep the old value before it changes (best effort: history never blocks a write)."""
        if self.history is None:
            return
        from uva.logging.events import current_trace_id

        try:
            await self.history.add({
                "key": item.key, "old_value": item.value, "old_category": item.category.value,
                "old_origin": item.origin, "superseded_at": utcnow(), "by_trace": current_trace_id(),
                "reason": reason,
            })
        except Exception as exc:
            log.warning("memory_history_failed", key=item.key, error=str(exc))

    async def changes(self, key: str, limit: int = 20) -> list[dict]:
        """Old values of ``key``, newest first."""
        if self.history is None:
            return []
        return await self.history.for_key(normalize_key(key), limit)

    async def touch(self, keys: Iterable[str], now: datetime | None = None) -> None:
        """Count a use of items that were shown (recall) or read (/memoria),
        in one write. The working set is not recomputed here (that keeps the
        system prompt stable); the new scores apply on the next recompute."""
        keys = sorted({normalize_key(k) for k in keys})
        if keys:
            await self.store.touch(keys, now or utcnow())

    async def _indexed(self, item: MemoryItem) -> None:
        if self.index is None:
            return
        try:
            await self.index.index_memory(item)
        except Exception as exc:  # the index is derived: /reindexar fixes it
            log.warning("index_memory_failed", key=item.key, error=str(exc))

    async def get(self, key: str) -> MemoryItem | None:
        item = await self.store.get(normalize_key(key))
        return None if item is None or item.is_expired(utcnow()) else item

    # --- working memory --------------------------------------------------------

    async def ranked(self, now: datetime | None = None) -> list[tuple[MemoryItem, float]]:
        now = now or utcnow()
        half_life = self.settings.memory_half_life_days
        items = [i for i in await self.store.all() if not i.is_expired(now)]
        return sorted(((i, score(i, now, half_life)) for i in items), key=lambda p: -p[1])

    async def recompute_working_set(self, now: datetime | None = None) -> list[MemoryItem]:
        now = now or utcnow()
        top = [item for item, _ in (await self.ranked(now))[: self.settings.memory_working_size]]
        await self.store.save_working_set([i.key for i in top], now)
        version = await self._bump_version()
        self._cache = (top, now, version)
        return top

    async def working_memory(self, now: datetime | None = None) -> list[MemoryItem]:
        """Current working set; recomputed if it is more than a day old, and
        reloaded when another process changed it (shared version counter)."""
        now = now or utcnow()
        version = await self._current_version()
        if self._cache and now - self._cache[1] < WORKING_SET_MAX_AGE and self._cache[2] == version:
            return self._cache[0]
        stored = await self.store.load_working_set()
        if stored is None or now - stored[1] >= WORKING_SET_MAX_AGE:
            return await self.recompute_working_set(now)
        keys, computed_at = stored
        items = [i for k in keys if (i := await self.store.get(k)) and not i.is_expired(now)]
        self._cache = (items, computed_at, version)
        return items

    async def _current_version(self) -> int | None:
        if self.version is None:
            return None
        try:
            return await self.version.get()
        except Exception as exc:  # Redis down: fall back to the 24h cache
            log.warning("memory_version_unavailable", error=str(exc))
            return self._cache[2] if self._cache else None

    async def _bump_version(self) -> int | None:
        if self.version is None:
            return None
        try:
            return await self.version.bump()
        except Exception as exc:
            log.warning("memory_version_unavailable", error=str(exc))
            return None

    # --- maintenance -------------------------------------------------------------

    async def maintenance(self, now: datetime | None = None) -> dict[str, int]:
        """Archive stale low-score items, then refresh the working set."""
        now = now or utcnow()
        cutoff = now - timedelta(days=self.settings.memory_archive_after_days)
        stale = [
            item
            for item, value in await self.ranked(now)
            if value < self.settings.memory_archive_score and item.last_used_at < cutoff
        ]
        if stale:
            await self.store.archive(stale, reason="low score, unused")
        working = await self.recompute_working_set(now)
        return {"archived": len(stale), "working_set": len(working)}


def render_memory(items: list[MemoryItem]) -> str:
    return "\n".join(f"- [{i.category.value}] {i.key}: {i.value}" for i in items)
