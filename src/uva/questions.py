"""Questions with options: UVA asks, the user taps an answer.

The ``ask`` action (and a blocked goal whose judge gave options) sends a
question with one button per option plus "something else", which waits for
a typed answer. Questions live in SQLite ``questions`` (migration 14) so the
worker can ask and the bot can take the answer; they expire after
:data:`TTL`. An answer to a conversation question goes back to the router as
his turn, with "Answer to your question: ..." in the context; an answer to a
goal's question resumes the goal and goes into its next run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from uva.clock import utcnow

TTL = timedelta(hours=24)
MAX_OPTIONS = 4
OPTION_CHARS = 40
OTHER = "o"


@dataclass(frozen=True, slots=True)
class Question:
    id: int
    question: str
    options: list[str]
    context: str
    goal_id: str
    created_at: datetime
    expires_at: datetime
    answered_at: datetime | None
    answer: str

    def open(self, now: datetime | None = None) -> bool:
        return self.answered_at is None and (now or utcnow()) < self.expires_at

    def context_line(self) -> str:
        """For the LLM, with his answer (English: only the LLM reads it)."""
        line = f"Answer to your question: {self.question}"
        return f"{line}\n({self.context})" if self.context else line


def _question(row) -> Question:
    return Question(
        row["id"], row["question"], json.loads(row["options_json"]), row["context"], row["goal_id"],
        datetime.fromisoformat(row["created_at"]), datetime.fromisoformat(row["expires_at"]),
        datetime.fromisoformat(row["answered_at"]) if row["answered_at"] else None, row["answer"],
    )


class QuestionStore:
    def __init__(self, db) -> None:
        self.db = db

    async def create(
        self, question: str, options: list[str], *, context: str = "", goal_id: str = "", now: datetime | None = None,
    ) -> Question:
        now = now or utcnow()
        clean = [" ".join(o.split())[:OPTION_CHARS] for o in options if o and o.strip()][:MAX_OPTIONS]
        if len(clean) < 2:
            raise ValueError("a question needs 2 to 4 options")
        args = (question.strip(), json.dumps(clean, ensure_ascii=False), context.strip(), goal_id,
                now.isoformat(), (now + TTL).isoformat())

        def job(conn):
            cursor = conn.execute(
                "INSERT INTO questions (question, options_json, context, goal_id, created_at, expires_at, answer) "
                "VALUES (?, ?, ?, ?, ?, ?, '')", args,
            )
            return _question(conn.execute("SELECT * FROM questions WHERE id = ?", (cursor.lastrowid,)).fetchone())

        return await self.db.run(job)

    async def get(self, question_id: int) -> Question | None:
        row = await self.db.run(lambda conn: conn.execute(
            "SELECT * FROM questions WHERE id = ?", (question_id,)).fetchone())
        return _question(row) if row else None

    async def answer(self, question_id: int, answer: str, now: datetime | None = None) -> bool:
        """Record the answer once; False when it was already answered or expired."""
        at = (now or utcnow()).isoformat()
        return bool(await self.db.run(lambda conn: conn.execute(
            "UPDATE questions SET answered_at = ?, answer = ? WHERE id = ? AND answered_at IS NULL "
            "AND expires_at > ?", (at, answer, question_id, at),
        ).rowcount))


def store_for(services) -> QuestionStore | None:
    return QuestionStore(services.sqlite) if services.sqlite is not None else None


def buttons(question: Question) -> list:
    from uva.i18n import t
    from uva.replies import Button

    out = [Button(option, f"ask:{question.id}:{index}") for index, option in enumerate(question.options)]
    out.append(Button(t("ask.other"), f"ask:{question.id}:{OTHER}"))
    return out
