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

from datetime import datetime, timedelta

from uva.clock import utcnow
from uva.config import Settings
from uva.memory.store import MemoryCategory, MemoryItem, MemoryStore, normalize_key, score

WORKING_SET_MAX_AGE = timedelta(hours=24)


class MemoryService:
    def __init__(self, store: MemoryStore, settings: Settings) -> None:
        self.store = store
        self.settings = settings
        self._cache: tuple[list[MemoryItem], datetime] | None = None

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
    ) -> MemoryItem:
        """Create or replace the value of ``key`` (keeps its history counters)."""
        now = utcnow()
        key = normalize_key(key)
        existing = await self.store.get(key)
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
        )
        await self.store.upsert(item)
        await self.recompute_working_set(now)
        return item

    async def update(
        self,
        key: str,
        *,
        value: str | None = None,
        relevance: float | None = None,
        category: MemoryCategory | None = None,
    ) -> MemoryItem | None:
        """Change fields of an existing item; counts as a use."""
        item = await self.store.get(normalize_key(key))
        if item is None:
            return None
        changes: dict = {"last_used_at": utcnow(), "use_count": item.use_count + 1}
        if value is not None:
            changes["value"] = value.strip()
        if relevance is not None:
            changes["relevance"] = relevance
        if category is not None:
            changes["category"] = category
        item = item.model_copy(update=changes)
        await self.store.upsert(item)
        await self.recompute_working_set()
        return item

    async def forget(self, key: str) -> bool:
        removed = await self.store.delete(normalize_key(key))
        if removed:
            await self.recompute_working_set()
        return removed

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
        self._cache = (top, now)
        return top

    async def working_memory(self, now: datetime | None = None) -> list[MemoryItem]:
        """Current working set; recomputed if it is more than a day old."""
        now = now or utcnow()
        if self._cache and now - self._cache[1] < WORKING_SET_MAX_AGE:
            return self._cache[0]
        stored = await self.store.load_working_set()
        if stored is None or now - stored[1] >= WORKING_SET_MAX_AGE:
            return await self.recompute_working_set(now)
        keys, computed_at = stored
        items = [i for k in keys if (i := await self.store.get(k)) and not i.is_expired(now)]
        self._cache = (items, computed_at)
        return items

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
            await self.store.archive(stale, reason="baixa nota sem uso")
        working = await self.recompute_working_set(now)
        return {"archived": len(stale), "working_set": len(working)}


def render_memory(items: list[MemoryItem]) -> str:
    return "\n".join(f"- [{i.category.value}] {i.key}: {i.value}" for i in items)
