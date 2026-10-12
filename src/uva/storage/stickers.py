"""Stickers the user taught the bot: what each one means and, optionally, an
action to run every time it is sent (table ``stickers`` in SQLite)."""

from __future__ import annotations

from dataclasses import dataclass

from uva.clock import utcnow
from uva.storage.sqlite import Database


@dataclass(frozen=True, slots=True)
class Sticker:
    id: int
    unique_id: str  # Telegram file_unique_id: the same sticker always has the same one
    file_id: str
    emoji: str
    set_name: str
    meaning: str
    action: str
    uses: int

    @property
    def label(self) -> str:
        return f"#{self.id} {self.emoji}".strip()


class StickerRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def get(self, unique_id: str) -> Sticker | None:
        def query(conn) -> Sticker | None:
            row = conn.execute("SELECT * FROM stickers WHERE unique_id = ?", (unique_id,)).fetchone()
            return _row(row) if row else None

        return await self.db.run(query)

    async def by_id(self, sticker_id: int) -> Sticker | None:
        def query(conn) -> Sticker | None:
            row = conn.execute("SELECT * FROM stickers WHERE id = ?", (sticker_id,)).fetchone()
            return _row(row) if row else None

        return await self.db.run(query)

    async def upsert(
        self, unique_id: str, *, file_id: str = "", emoji: str = "", set_name: str = "",
        meaning: str | None = None, action: str | None = None,
    ) -> Sticker:
        """Create the sticker or update the given fields (None = keep)."""
        now = utcnow().isoformat()

        def write(conn) -> None:
            conn.execute(
                "INSERT INTO stickers (unique_id, file_id, emoji, set_name, meaning, action, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(unique_id) DO UPDATE SET"
                "  file_id = COALESCE(NULLIF(excluded.file_id, ''), file_id),"
                "  emoji = COALESCE(NULLIF(excluded.emoji, ''), emoji),"
                "  set_name = COALESCE(NULLIF(excluded.set_name, ''), set_name),"
                "  meaning = CASE WHEN ? THEN excluded.meaning ELSE meaning END,"
                "  action = CASE WHEN ? THEN excluded.action ELSE action END,"
                "  updated_at = excluded.updated_at",
                (unique_id, file_id, emoji, set_name, meaning or "", action or "", now, now,
                 meaning is not None, action is not None),
            )

        await self.db.run(write)
        sticker = await self.get(unique_id)
        assert sticker is not None
        return sticker

    async def update(self, sticker_id: int, *, meaning: str | None = None, action: str | None = None) -> Sticker | None:
        current = await self.by_id(sticker_id)
        if current is None:
            return None
        return await self.upsert(current.unique_id, meaning=meaning, action=action)

    async def delete(self, sticker_id: int) -> bool:
        def write(conn) -> int:
            return conn.execute("DELETE FROM stickers WHERE id = ?", (sticker_id,)).rowcount

        return bool(await self.db.run(write))

    async def used(self, unique_id: str) -> None:
        def write(conn) -> None:
            conn.execute("UPDATE stickers SET uses = uses + 1 WHERE unique_id = ?", (unique_id,))

        await self.db.run(write)

    async def all(self) -> list[Sticker]:
        def query(conn) -> list[Sticker]:
            return [_row(r) for r in conn.execute("SELECT * FROM stickers ORDER BY id").fetchall()]

        return await self.db.run(query)


def _row(r) -> Sticker:
    return Sticker(r["id"], r["unique_id"], r["file_id"], r["emoji"], r["set_name"], r["meaning"], r["action"], r["uses"])
