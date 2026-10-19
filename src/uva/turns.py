"""The turns being handled right now, so /parar can stop one.

The transport (main.py) runs each incoming message through :meth:`run`; the
task is registered while it works. :meth:`stop` cancels it, and :meth:`run`
then returns a short "stopped" reply instead of raising.
"""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass, field
from datetime import datetime

from uva.clock import utcnow
from uva.i18n import t
from uva.replies import Reply


@dataclass
class Turn:
    id: str
    label: str
    task: asyncio.Task
    started_at: datetime = field(default_factory=utcnow)
    stopped: bool = False


class TurnTracker:
    def __init__(self) -> None:
        self._turns: dict[str, Turn] = {}

    def running(self) -> list[Turn]:
        return [turn for turn in self._turns.values() if not turn.task.done()]

    async def run(self, work, label: str):
        """Await ``work`` (a coroutine) as a stoppable turn."""
        task = asyncio.ensure_future(work)
        turn = Turn(secrets.token_hex(3), label[:80], task)
        self._turns[turn.id] = turn
        try:
            return await task
        except asyncio.CancelledError:
            if not turn.stopped:
                raise  # cancelled by someone else (shutdown): let it propagate
            return Reply(t("commands.stop.stopped_turn", label=turn.label))
        finally:
            self._turns.pop(turn.id, None)

    def stop(self, turn_id: str) -> Turn | None:
        turn = self._turns.get(turn_id)
        if turn is None or turn.task.done():
            return None
        turn.stopped = True
        turn.task.cancel()
        return turn
