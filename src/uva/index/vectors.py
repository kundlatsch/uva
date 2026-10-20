"""Hybrid search (optional, SEARCH_EMBEDDINGS=on): embeddings next to the FTS index.

Notes (in ~800-character chunks) and memory items get an embedding from an
OpenAI-compatible endpoint (EMBEDDING_MODEL, cost counted under the
``embedding`` purpose). Embeddings are made in batches when the index syncs,
never on the reply path except the one for the query. They are kept in SQLite
``vec_chunks``; with the sqlite-vec extension loaded the nearest chunks come
from its ``search_vec`` vec0 table, otherwise from a plain scan.

Ranking (:func:`rank`): ``0.6 × bm25 (normalized) + 0.4 × cosine``, times a
recency decay with a 30-day half-life for memory items, then MMR (λ 0.7) so
four chunks of the same note don't crowd out the rest. Embeddings off, a
provider that isn't OpenAI-compatible or a failed call: FTS only, as before.
"""

from __future__ import annotations

import hashlib
import math
import sqlite3
import struct
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from uva.clock import utcnow
from uva.logging import get_logger

log = get_logger(__name__)

KINDS = ("note", "memory")
CHUNK_CHARS = 800
BATCH = 64
BM25_WEIGHT, COSINE_WEIGHT = 0.6, 0.4
HALF_LIFE_DAYS = 30
MMR_LAMBDA = 0.7
# A chunk found only by meaning must be at least this close to count.
MIN_COSINE = 0.3
DIM_KEY = "vectors:dim"

SCHEMA = """
CREATE TABLE IF NOT EXISTS vec_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    ref TEXT NOT NULL,
    chunk INTEGER NOT NULL,
    hash TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT '',
    embedding BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vec_chunks_ref ON vec_chunks (kind, ref);
"""


class Embedder(Protocol):
    model: str

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


def chunks(text: str, size: int = CHUNK_CHARS) -> list[str]:
    """~``size``-character pieces cut at paragraph or line breaks when possible."""
    text = text.strip()
    out: list[str] = []
    while len(text) > size:
        cut = max(text.rfind("\n\n", 0, size), text.rfind("\n", 0, size), text.rfind(". ", 0, size))
        cut = cut + 1 if cut > size // 3 else size
        out.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        out.append(text)
    return out


def _unit(vector: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector)) or 1.0
    return [x / norm for x in vector]


def _pack(vector: Sequence[float]) -> bytes:
    return struct.pack(f"{len(vector)}f", *vector)


def _unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"{len(blob) // 4}f", blob))


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b))  # both are unit vectors


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class VecHit:
    kind: str
    ref: str
    cosine: float
    vector: list[float]
    updated_at: str = ""


@dataclass(frozen=True, slots=True)
class Ranked:
    kind: str
    ref: str
    score: float


def _load_extension(conn: sqlite3.Connection) -> bool:
    try:
        import sqlite_vec

        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return True
    except Exception:  # no extension support in this Python, or no package
        return False


class VectorIndex:
    def __init__(self, db, embedder: Embedder) -> None:
        self.db = db
        self.embedder = embedder
        self.native: bool | None = None  # sqlite-vec loaded (None: not tried yet)

    # --- storage ------------------------------------------------------------------------

    def _setup(self, conn: sqlite3.Connection, dim: int | None = None) -> None:
        conn.executescript(SCHEMA)
        if self.native is None:
            self.native = _load_extension(conn)
        elif self.native:
            _load_extension(conn)
        if self.native and dim:
            conn.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS search_vec USING vec0(embedding float[{dim}])")

    async def count(self) -> int:
        def job(conn):
            self._setup(conn)
            return conn.execute("SELECT COUNT(*) FROM vec_chunks").fetchone()[0]

        return await self.db.run(job)

    async def clear(self) -> None:
        def job(conn):
            self._setup(conn)
            conn.execute("DELETE FROM vec_chunks")
            if self.native:
                conn.execute("DROP TABLE IF EXISTS search_vec")
            conn.execute("DELETE FROM kv WHERE key = ?", (DIM_KEY,))

        await self.db.run(job)

    async def sync(self) -> dict[str, int]:
        """Embed what is new or changed in the FTS index (notes, memory); drop the gone."""
        def plan(conn):
            self._setup(conn)
            marks = ", ".join("?" for _ in KINDS)
            rows = conn.execute(
                f"SELECT kind, ref, title, body, updated_at FROM search_index WHERE kind IN ({marks})", KINDS,
            ).fetchall()
            have = {}
            for r in conn.execute("SELECT kind, ref, chunk, hash FROM vec_chunks").fetchall():
                have.setdefault((r["kind"], r["ref"]), {})[r["chunk"]] = r["hash"]
            todo, keep = [], set()
            for row in rows:
                key = (row["kind"], row["ref"])
                keep.add(key)
                pieces = chunks(f"{row['title']}\n{row['body']}")
                hashes = [_hash(p) for p in pieces]
                if have.get(key) == dict(enumerate(hashes)):
                    continue
                todo.append((key, pieces, hashes, row["updated_at"] or ""))
            gone = [key for key in have if key not in keep]
            return todo, gone

        todo, gone = await self.db.run(plan)
        texts = [piece for _, pieces, _, _ in todo for piece in pieces]
        vectors: list[list[float]] = []
        for start in range(0, len(texts), BATCH):
            vectors += [_unit(v) for v in await self.embedder.embed(texts[start:start + BATCH])]

        def write(conn):
            self._setup(conn, len(vectors[0]) if vectors else None)
            for key in gone + [key for key, *_ in todo]:
                self._drop(conn, *key)
            cursor = iter(vectors)
            for (kind, ref), pieces, hashes, updated in todo:
                for number, (piece, digest) in enumerate(zip(pieces, hashes)):
                    vector = next(cursor)
                    blob = _pack(vector)
                    row_id = conn.execute(
                        "INSERT INTO vec_chunks (kind, ref, chunk, hash, updated_at, embedding) VALUES (?, ?, ?, ?, ?, ?)",
                        (kind, ref, number, digest, updated, blob),
                    ).lastrowid
                    if self.native:
                        conn.execute("INSERT INTO search_vec (rowid, embedding) VALUES (?, ?)", (row_id, blob))
            if vectors:
                conn.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (DIM_KEY, str(len(vectors[0]))))

        await self.db.run(write)
        return {"embedded": len(texts), "removed": len(gone)}

    def _drop(self, conn, kind: str, ref: str) -> None:
        ids = [r[0] for r in conn.execute("SELECT id FROM vec_chunks WHERE kind = ? AND ref = ?", (kind, ref))]
        if not ids:
            return
        conn.execute("DELETE FROM vec_chunks WHERE kind = ? AND ref = ?", (kind, ref))
        if self.native:
            conn.executemany("DELETE FROM search_vec WHERE rowid = ?", [(i,) for i in ids])

    async def rebuild(self) -> dict[str, int]:
        await self.clear()
        return await self.sync()

    # --- query -----------------------------------------------------------------------------

    async def nearest(self, text: str, kinds: Iterable[str], limit: int = 20) -> list[VecHit]:
        """The chunks closest to ``text`` ([] when the embedding call fails)."""
        try:
            query = _unit((await self.embedder.embed([text]))[0])
        except Exception as exc:
            log.warning("embedding_failed", error=str(exc))
            return []
        kinds = tuple(kinds)

        def job(conn):
            self._setup(conn)
            if self.native and conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'search_vec'"
            ).fetchone():
                rows = conn.execute(
                    "SELECT c.kind, c.ref, c.updated_at, c.embedding FROM search_vec v JOIN vec_chunks c "
                    "ON c.id = v.rowid WHERE v.embedding MATCH ? AND k = ? ORDER BY v.distance",
                    (_pack(query), limit * 3),
                ).fetchall()
            else:
                rows = conn.execute("SELECT kind, ref, updated_at, embedding FROM vec_chunks").fetchall()
            hits = []
            for row in rows:
                if row["kind"] not in kinds:
                    continue
                vector = _unpack(row["embedding"])
                hits.append(VecHit(row["kind"], row["ref"], cosine(query, vector), vector, row["updated_at"]))
            hits.sort(key=lambda h: -h.cosine)
            return hits[: limit * 3]

        return await self.db.run(job)


def _age_days(updated_at: str, now: datetime) -> float:
    try:
        at = datetime.fromisoformat(updated_at)
    except ValueError:
        return 0.0
    at = at if at.tzinfo else at.replace(tzinfo=UTC)
    return max(0.0, (now - at).total_seconds() / 86400)


def rank(fts_hits, vec_hits: list[VecHit], *, limit: int, now: datetime | None = None) -> list[Ranked]:
    """Hybrid score per (kind, ref), then MMR so one note doesn't fill the list.

    ``fts_hits`` are index Hits (bm25 rank: lower is better)."""
    now = now or utcnow()
    bm25: dict[tuple[str, str], float] = {}
    if fts_hits:
        ranks = [-h.rank for h in fts_hits]  # higher is better
        low, high = min(ranks), max(ranks)
        for hit, value in zip(fts_hits, ranks):
            bm25[(hit.kind, hit.ref)] = 1.0 if high == low else (value - low) / (high - low)
    best: dict[tuple[str, str], VecHit] = {}
    updated: dict[tuple[str, str], str] = {(h.kind, h.ref): h.updated_at for h in fts_hits}
    for hit in vec_hits:
        key = (hit.kind, hit.ref)
        updated.setdefault(key, hit.updated_at)
        if key not in best or hit.cosine > best[key].cosine:
            best[key] = hit
    candidates = []
    for key in set(bm25) | {k for k, h in best.items() if h.cosine >= MIN_COSINE}:
        score = BM25_WEIGHT * bm25.get(key, 0.0) + COSINE_WEIGHT * max(0.0, best[key].cosine if key in best else 0.0)
        if key[0] == "memory":
            score *= 0.5 ** (_age_days(updated.get(key, ""), now) / HALF_LIFE_DAYS)
        candidates.append((key, score))
    # MMR: relevance minus similarity to what is already chosen.
    chosen: list[tuple[tuple[str, str], float]] = []
    pool = sorted(candidates, key=lambda c: -c[1])
    while pool and len(chosen) < limit:
        def mmr(item):
            key, score = item
            similar = 0.0
            for other, _ in chosen:
                if other == key:
                    similar = 1.0
                elif key in best and other in best:
                    similar = max(similar, cosine(best[key].vector, best[other].vector))
            return MMR_LAMBDA * score - (1 - MMR_LAMBDA) * similar

        pick = max(pool, key=mmr)
        pool.remove(pick)
        chosen.append(pick)
    return [Ranked(kind, ref, score) for (kind, ref), score in chosen]


# --- the embedding endpoint -------------------------------------------------------------------


class OpenAIEmbedder:
    """OpenAI-compatible /embeddings; every call is priced and logged (purpose "embedding")."""

    def __init__(self, settings, usage=None) -> None:
        from openai import AsyncOpenAI

        key = settings.embedding_api_key or settings.llm_api_key
        self.client = AsyncOpenAI(
            api_key=key.get_secret_value() if key else None,
            base_url=settings.embedding_base_url or settings.llm_base_url, max_retries=1, timeout=30,
        )
        self.model = settings.embedding_model
        self.price = settings.embedding_price_per_1m
        self.usage = usage

    async def embed(self, texts: list[str]) -> list[list[float]]:
        from uva.llm.budget import Scope, UsageRecord
        from uva.logging.events import EventType, current_trace_id, emit

        response = await self.client.embeddings.create(model=self.model, input=texts)
        tokens = getattr(getattr(response, "usage", None), "prompt_tokens", 0) or 0
        cost = tokens * self.price / 1_000_000
        if self.usage is not None:
            try:
                await self.usage.add(UsageRecord(
                    provider="openai_compatible", model=self.model, input_tokens=tokens, output_tokens=0,
                    cost_usd=cost, scope=Scope.PROACTIVE, purpose="embedding", trace_id=current_trace_id(),
                ))
            except Exception as exc:
                log.warning("usage_store_unavailable", error=str(exc))
        await emit(EventType.LLM_CALL, provider="openai_compatible", model=self.model, purpose="embedding",
                   kind="embedding", input_tokens=tokens, output_tokens=0, cost_usd=round(cost, 6),
                   texts=len(texts))
        return [list(item.embedding) for item in response.data]


def build_vectors(settings, db, usage=None) -> VectorIndex | None:
    """None unless SEARCH_EMBEDDINGS is on and there is an OpenAI-compatible endpoint."""
    from uva.config import LLMProvider

    if not settings.search_embeddings or db is None:
        return None
    openai_like = settings.llm_provider is LLMProvider.OPENAI_COMPATIBLE or settings.embedding_base_url
    if not (settings.embedding_api_key or (openai_like and settings.llm_api_key)):
        log.info("embeddings_off", reason="no OpenAI-compatible endpoint (EMBEDDING_API_KEY)")
        return None
    return VectorIndex(db, OpenAIEmbedder(settings, usage))
