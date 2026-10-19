"""Expenses: a small data-access layer over the ``expenses`` table."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from uva.clock import utcnow
from uva.storage.sqlite import Database
from uva.text import normalize


@dataclass(frozen=True, slots=True)
class Expense:
    id: int
    amount_cents: int
    category: str
    description: str
    spent_at: datetime


class ExpenseRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def add(
        self,
        amount_cents: int,
        category: str,
        description: str = "",
        spent_at: datetime | None = None,
    ) -> Expense:
        if amount_cents <= 0:
            raise ValueError("the amount must be positive")
        category = normalize_category(category)
        spent_at = spent_at or utcnow()

        def insert(conn) -> int:
            cursor = conn.execute(
                "INSERT INTO expenses (amount_cents, category, description, spent_at, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (amount_cents, category, description.strip(), _iso(spent_at), _iso(utcnow())),
            )
            return cursor.lastrowid

        expense_id = await self.db.run(insert)
        return Expense(expense_id, amount_cents, category, description.strip(), spent_at)

    async def totals_by_category(
        self, since: datetime | None = None, until: datetime | None = None
    ) -> list[tuple[str, int]]:
        """(category, total cents), largest first."""
        where, params = _period(since, until)

        def query(conn) -> list[tuple[str, int]]:
            rows = conn.execute(
                f"SELECT category, SUM(amount_cents) AS total FROM expenses {where}"
                " GROUP BY category ORDER BY total DESC",
                params,
            ).fetchall()
            return [(row["category"], row["total"]) for row in rows]

        return await self.db.run(query)

    async def recent(self, limit: int = 10) -> list[Expense]:
        def query(conn) -> list[Expense]:
            rows = conn.execute(
                "SELECT * FROM expenses ORDER BY spent_at DESC, id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [
                Expense(
                    r["id"],
                    r["amount_cents"],
                    r["category"],
                    r["description"],
                    datetime.fromisoformat(r["spent_at"]),
                )
                for r in rows
            ]

        return await self.db.run(query)


def normalize_category(category: str) -> str:
    return normalize(category).replace(" ", "_") or "outros"


_AMOUNT = re.compile(r"^(?:r\$)?\s*(\d{1,3}(?:\.\d{3})+|\d+)(?:[.,](\d{1,2}))?$", re.IGNORECASE)


def parse_amount(text: str) -> int:
    """'25' -> 2500, '25,5' -> 2550, 'R$1.234,56' -> 123456 (cents)."""
    cleaned = text.strip().replace(" ", "")
    match = _AMOUNT.match(cleaned)
    if not match:
        raise ValueError(f"invalid amount: {text!r}")
    whole = int(match[1].replace(".", ""))
    cents = int((match[2] or "0").ljust(2, "0"))
    return whole * 100 + cents


def format_brl(cents: int) -> str:
    """123456 -> 'R$ 1.234,56'."""
    whole, rest = divmod(cents, 100)
    return f"R$ {whole:,}".replace(",", ".") + f",{rest:02d}"


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def _period(since: datetime | None, until: datetime | None) -> tuple[str, tuple]:
    clauses, params = [], []
    if since is not None:
        clauses.append("spent_at >= ?")
        params.append(_iso(since))
    if until is not None:
        clauses.append("spent_at < ?")
        params.append(_iso(until))
    return ("WHERE " + " AND ".join(clauses)) if clauses else "", tuple(params)
