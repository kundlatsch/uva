import asyncio
import json

from fakes import with_llm

from uva import progress
from uva.progress import LiveSink, with_feedback
from uva.replies import Reply
from uva.router import Router
from uva.telegram_io import respond


class FakeSink(LiveSink):
    def __init__(self):
        super().__init__(lambda category: f"<{category}>")
        self.log: list[tuple] = []
        self.n = 0

    async def _post(self, text):
        self.n += 1
        self.log.append(("post", self.n, text))
        return self.n

    async def _edit(self, handle, text, buttons=None):
        self.log.append(("edit", handle, text))

    async def _post_reply(self, reply):
        self.n += 1
        self.log.append(("post", self.n, reply.text))


async def test_fast_work_shows_no_placeholder():
    sink = FakeSink()

    async def work():
        return Reply("pong")

    reply = await with_feedback(sink, work())
    await sink.deliver(reply.messages())
    assert sink.log == [("post", 1, "pong")]


async def test_slow_work_gets_a_placeholder_that_becomes_the_reply(monkeypatch):
    monkeypatch.setattr(progress, "PLACEHOLDER_AFTER", 0.01)
    sink = FakeSink()

    async def work():
        await asyncio.sleep(0.05)
        await progress.activity("procurando")
        return Reply.from_parts(["resposta", "detalhe"])

    reply = await with_feedback(sink, work())
    await sink.deliver(reply.messages())
    assert sink.log == [
        ("post", 1, "<pensando>"), ("edit", 1, "<procurando>"), ("edit", 1, "resposta"), ("post", 2, "detalhe"),
    ]


async def test_llm_reply_goes_out_before_slow_actions(services, monkeypatch):
    import httpx

    from uva.shell.service import ShellService

    def handler(request):
        return httpx.Response(200, json={"exists": [], "exit": 0, "output": "1267650600228229401496703205376\n", "seconds": 0.1})

    services.shell = ShellService(services.settings, transport=httpx.MockTransport(handler))
    plan = {"actions": [{"type": "python_run", "params": {"code": "print(2**100)"}}], "reply": "Vou calcular na minha pasta."}
    with_llm(services, json.dumps(plan))
    sink = FakeSink()
    reply = await with_feedback(sink, Router(services).handle_text("quanto é 2 elevado a 100?"))
    await sink.deliver(reply.messages())
    texts = [entry[2] for entry in sink.log]
    assert texts[0] == "Vou calcular na minha pasta."  # before the work
    assert texts[1] == "<executando>"  # placeholder for the slow part...
    assert "1267650600228229401496703205376" in texts[2] and sink.log[2][0] == "edit"  # ...replaced by the result
    assert "Vou calcular" not in texts[2]  # not said twice


class FakeBot:
    def __init__(self):
        self.calls: list[tuple] = []
        self.next_id = 0

    async def send_chat_action(self, chat_id, action):
        self.calls.append(("typing",))

    async def send_message(self, chat_id, text, reply_markup=None, parse_mode=None):
        self.next_id += 1
        self.calls.append(("send", text))

        class M:
            message_id = self.next_id

        return M()

    async def edit_message_text(self, text, chat_id, message_id, reply_markup=None, parse_mode=None):
        self.calls.append(("edit", message_id, text))


async def test_telegram_typing_first_then_placeholder_edited(monkeypatch):
    monkeypatch.setattr(progress, "PLACEHOLDER_AFTER", 0.01)
    bot = FakeBot()

    async def work():
        await asyncio.sleep(0.05)
        return Reply("**pronto**")

    await respond(bot, 1, lambda c: f"[{c}]", work(), activity="executando")
    assert bot.calls[0] == ("typing",)
    assert ("send", "[executando]") in bot.calls
    assert bot.calls[-1] == ("edit", 1, "<b>pronto</b>")
