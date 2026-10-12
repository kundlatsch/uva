"""Ask the bot a question from the command line, through the real router.

    docker compose exec bot python scripts/ask.py "me fala sobre o livro X"

Prints the reply parts and what the LLM planned (from the events log). The
exchange is NOT added to the short-term memory (so test questions don't pile
up in the real conversation); pass --record to keep it.
"""

from __future__ import annotations

import asyncio
import json
import sys

from uva.bootstrap import build_services, setup_storage
from uva.config import get_settings
from uva.logging import events
from uva.logging.events import EventType
from uva.router import Router


class Tap:
    """Wraps the event store to print routing and action events as they happen."""

    def __init__(self, inner) -> None:
        self.inner = inner

    async def add(self, event) -> None:
        if event.type in (EventType.ROUTE, EventType.ACTION, EventType.ERROR, EventType.LLM_CALL):
            data = {k: v for k, v in event.data.items() if k not in ("raw",)}
            print(f"[{event.type.value}] {json.dumps(data, ensure_ascii=False, default=str)[:700]}")
        if self.inner is not None:
            await self.inner.add(event)

    def __getattr__(self, name):
        return getattr(self.inner, name)


async def main(text: str, record: bool) -> None:
    services = build_services(get_settings())
    await setup_storage(services)
    if services.stm is not None and not record:

        async def skip(*args, **kwargs) -> None:
            pass

        services.stm.add_exchange = skip
    events.set_store(Tap(events._store))
    reply = await Router(services).handle_text(text)
    print("\n=== reply ===")
    for part in reply.parts or [reply.text]:
        print(part)
        print("---")


if __name__ == "__main__":
    args = sys.argv[1:]
    asyncio.run(main(" ".join(a for a in args if a != "--record"), "--record" in args))
