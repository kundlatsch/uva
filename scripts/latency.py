"""Time to first message for each routing path, through the real router.

    docker compose exec bot python /repo/scripts/latency.py

Measures how long the user waits for the FIRST thing to appear (a placeholder
counts) and for the final reply. The exchanges are not kept in the
short-term memory. LLM paths spend a little real budget (~US$ 0.2 in all);
the daily limit is raised in this process only.
"""

from __future__ import annotations

import asyncio
import time

from uva.bootstrap import build_services, setup_storage
from uva.config import get_settings
from uva.progress import LiveSink, with_feedback
from uva.router import Router

CASES = [
    ("command", "/ping"),
    ("command (lista)", "/objetivos"),
    ("similarity", "meus objetivos"),
    ("llm reply", "me diz uma curiosidade curta sobre uvas"),
    ("llm + recall", "qual livro eu deixei pela metade?"),
    ("llm -> search (task)", "pesquisa as últimas notícias sobre o Prêmio Jabuti 2026"),
    ("llm -> shell", "calcula na sua pasta quanto é 2 elevado a 100 usando python"),
    ("/sh", "/sh sleep 2; echo pronto"),
    ("/py", "/py import time; time.sleep(1); print(42)"),
]


class Sink(LiveSink):
    """Records when each placeholder/message would appear on Telegram."""

    def __init__(self, say, started: float, activity: str) -> None:
        super().__init__(say, activity)
        self.started = started
        self.first: float | None = None
        self.items: list[str] = []

    def _mark(self, text: str) -> None:
        if self.first is None:
            self.first = time.perf_counter() - self.started
        self.items.append(text)

    async def _post(self, text: str):
        self._mark(text)
        return len(self.items)

    async def _edit(self, handle, text: str, buttons=None) -> None:
        self.items.append(f"(edit) {text}")

    async def _post_reply(self, reply) -> None:
        self._mark(reply.text)


async def main() -> None:
    services = build_services(get_settings())
    await setup_storage(services)
    services.settings.llm_daily_budget_usd += 5  # this process only

    async def skip(*a, **k) -> None:
        pass

    services.stm.add_exchange = skip
    router = Router(services)
    print(f"{'path':24} {'first':>7} {'final':>7}  first thing shown")
    for label, text in CASES:
        started = time.perf_counter()
        sink = Sink(services.say, started, "executando" if text.startswith("/") else "pensando")
        reply = await with_feedback(sink, router.handle_text(text))
        await sink.deliver(reply.messages())
        final = time.perf_counter() - started
        first = sink.first if sink.first is not None else final
        shown = (sink.items[0] if sink.items else reply.text).splitlines()[0][:70]
        shown += f"  [{len(sink.items)} passos]"
        print(f"{label:24} {first:6.2f}s {final:6.2f}s  {shown}")


if __name__ == "__main__":
    asyncio.run(main())
