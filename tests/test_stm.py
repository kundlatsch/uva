import json
from datetime import timedelta

import pytest
from fakes import FakeBusy, FakeSearch, FakeTaskQueue, InMemoryMemoryStore, make_settings, with_llm

from uva.clock import utcnow
from uva.llm.budget import Scope, UsageRecord
from uva.matching import status_question
from uva.memory import MemoryService
from uva.router import Router
from uva.search.base import SearchResult
from uva.stm.promote import promote_idle_sessions
from uva.stm.service import ShortTermMemory
from uva.stm.session import Ref, Session, Turn
from uva.tasks.queue import TaskStatus
from uva.tasks.runner import run_action_task


class InMemorySessionBackend:
    def __init__(self) -> None:
        self.data: dict[int, str] = {}
        self.persisted: dict[int, bool] = {}

    async def load(self, user_id):
        raw = self.data.get(user_id)
        return Session.model_validate_json(raw) if raw else None

    async def update(self, user_id, change, ttl):
        session = await self.load(user_id) or Session()
        change(session)
        self.data[user_id] = session.model_dump_json()
        self.persisted[user_id] = session.has_active_refs
        return session

    async def delete(self, user_id):
        self.data.pop(user_id, None)

    async def users(self):
        return list(self.data)


@pytest.fixture
def stm(services) -> ShortTermMemory:
    services.stm = ShortTermMemory(InMemorySessionBackend(), services.settings)
    services.tasks = FakeTaskQueue()
    services.busy = FakeBusy()
    return services.stm


SEARCH_PLAN = json.dumps(
    {"actions": [{"type": "search", "params": {"query": "Fórmula 1"}}], "reply": "Vou pesquisar."}
)


def test_trim_limits_and_ttl():
    now = utcnow()
    session = Session(
        turns=[Turn(role="user", text=str(i), at=now - timedelta(minutes=40 - i)) for i in range(40)]
        + [Turn(role="user", text="velho", at=now - timedelta(hours=20))],
        refs=[
            Ref(ref="t1", tipo="pesquisa", id="a", rotulo="velha ok", updated=now - timedelta(hours=20)),
            Ref(ref="t2", tipo="tarefa", id="b", rotulo="velha rodando", status="rodando",
                updated=now - timedelta(hours=20)),
        ]
        + [Ref(ref=f"t{i}", tipo="nota", id=str(i), rotulo="n") for i in range(3, 20)],
        focus="t1",
    )
    session.trim(now, timedelta(hours=12), max_turns=30, max_refs=15)
    assert len(session.turns) == 30 and all(t.text != "velho" for t in session.turns)
    handles = [r.ref for r in session.refs]
    assert "t1" not in handles  # finished and expired
    assert "t2" in handles  # still running: never dropped
    assert len(session.refs) == 15
    assert session.focus == session.refs[-1].ref  # focus moved off the dropped ref


def test_focus_resolution():
    session = Session(
        refs=[
            Ref(ref="t1", tipo="pesquisa", id="aaa", rotulo="pesquisa: Fórmula 1"),
            Ref(ref="t2", tipo="nota", id="00-inbox/x.md", rotulo="nota: compras"),
        ],
        focus="t2",
    )
    assert session.find().ref == "t2"
    assert session.find(tipo="pesquisa").ref == "t1"
    assert session.find("t1").ref == "t1"
    assert session.find("aaa").ref == "t1"
    assert session.find("compras").ref == "t2"
    assert session.find("inexistente") is None


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("como tá aquela pesquisa?", "pesquisa"),
        ("e a pesquisa?", "pesquisa"),
        ("e aí, como tá aquela task?", "tarefa"),
        ("deu certo?", None),
        ("terminou?", None),
        ("como tá aquela?", None),
    ],
)
def test_status_questions(text, kind):
    question = status_question(text)
    assert question is not None and question.kind == kind


@pytest.mark.parametrize(
    "text",
    ["e a capital da França?", "pesquisa sobre a história do Brasil e da economia mundial", "oi"],
)
def test_not_status_questions(text):
    assert status_question(text) is None


async def test_router_records_turns_with_command_summaries(services, stm):
    router = Router(services)
    await router.handle_text("/ping")
    session = await stm.session()
    assert [(t.role, t.text) for t in session.turns] == [("user", "/ping"), ("bot", "/ping → pong")]


async def test_search_creates_ref_and_worker_updates_it(services, stm):
    services.search = {"web": FakeSearch("web", SearchResult("F1", "https://f1.com", "x"))}
    with_llm(services, SEARCH_PLAN, "Resumo da F1.")
    await Router(services).handle_text("pesquisa sobre Fórmula 1")

    session = await stm.session()
    ref = session.refs[-1]
    assert (ref.tipo, ref.rotulo, ref.status) == ("pesquisa", "pesquisa: Fórmula 1", "na fila")
    assert session.focus == ref.ref

    record = services.tasks.sent[0]
    sent = []

    async def notify(reply):
        sent.append(reply.text)

    await run_action_task(
        services, notify, task_id=record.id, action_name="search", params=record.params,
        trace_id=None, attempt=0, max_retries=3,
    )
    assert (await stm.session()).refs[-1].status == "ok"


async def test_context_block_goes_into_the_prompt(services, stm):
    await stm.add_ref("pesquisa", "abc123", "pesquisa: Fórmula 1", "rodando")
    await stm.add_turn("notice", "🎯 Objetivo X rodou")
    provider, _ = with_llm(services, json.dumps({"actions": [], "reply": "ok"}))
    await Router(services).handle_text("me lembra o que você fez hoje")
    system = provider.calls[0]["system"]
    assert "Recent context" in system
    assert "t1 [pesquisa] pesquisa: Fórmula 1 (id abc123" in system and "← em foco" in system
    assert "🎯 Objetivo X rodou" in system
    # The notice is also part of the conversation history sent as messages.
    assert any(m.content.startswith("[aviso que eu mandei]") for m in provider.calls[0]["messages"])


async def test_status_question_is_answered_without_the_llm(services, stm):
    record = await services.tasks.enqueue("search", {"query": "Fórmula 1"}, None)
    await stm.add_ref("pesquisa", record.id, "pesquisa: Fórmula 1", "na fila")
    await services.tasks.store.update(record.id, status=TaskStatus.DONE)
    provider, _ = with_llm(services)

    reply = (await Router(services).handle_text("como tá aquela pesquisa?")).text

    assert provider.calls == []
    phrase, facts = reply.split("\n")
    assert phrase in services.phrases.categories["tarefa_ok"] or record.id in phrase
    assert facts.startswith(f"pesquisa: Fórmula 1 (t1, id {record.id}) terminou")
    assert (await stm.session()).refs[-1].status == "ok"  # synced from the real record


async def test_status_question_without_refs_goes_to_the_llm(services, stm):
    provider, _ = with_llm(services, json.dumps({"actions": [], "reply": "De qual?"}))
    assert (await Router(services).handle_text("deu certo?")).text == "De qual?"
    assert len(provider.calls) == 1


async def test_retry_and_cancel_actions(services, stm):
    failed = await services.tasks.enqueue("search", {"query": "F1"}, None)
    await services.tasks.store.update(failed.id, status=TaskStatus.FAILED, error="timeout")
    await stm.add_ref("pesquisa", failed.id, "pesquisa: F1", "falhou", )
    with_llm(
        services,
        json.dumps({"actions": [{"type": "retry", "params": {}}], "reply": ""}),
        json.dumps({"actions": [{"type": "cancel", "params": {}}], "reply": ""}),
    )
    router = Router(services)
    reply = (await router.handle_text("tenta de novo")).text
    assert reply.startswith("🔁 Rodando de novo: pesquisa: F1")
    new = services.tasks.sent[-1]
    assert new.id != failed.id and new.params == {"query": "F1"}
    session = await stm.session()
    assert session.focus == session.refs[-1].ref and session.refs[-1].id == new.id

    reply = (await router.handle_text("cancela isso")).text
    assert "(pesquisa: F1 cancelada)" in reply
    assert services.tasks.store.records[new.id].status is TaskStatus.CANCELLED


async def test_contexto_command(services, stm):
    router = Router(services)
    assert "Nada no contexto" in (await router.handle_text("/contexto")).text
    await stm.add_ref("nota", "00-inbox/x.md", "nota: compras")
    text = (await router.handle_text("/contexto")).text
    assert "nota: compras" in text and "Últimos turnos" in text
    assert "apagado" in (await router.handle_text("/contexto limpar")).text
    session = await stm.session()
    assert session.refs == [] and len(session.turns) == 2  # only the "limpar" exchange


async def test_promotion_saves_facts_and_discards_session(services, stm):
    services.memory = MemoryService(InMemoryMemoryStore(), make_settings())
    provider, _ = with_llm(
        services,
        json.dumps({"memories": [{"key": "time", "value": "Torce pro Avaí", "category": "perfil", "relevance": 0.7}]}),
    )
    await stm.add_exchange("eu torço pro Avaí, viu", "Anotado, sofredor.")
    await stm.add_ref("tarefa", "run1", "tarefa: algo", "rodando")
    later = utcnow() + timedelta(hours=4)

    result = await promote_idle_sessions(services, now=later)

    assert result["promoted"] == 1 and result["saved"] == 1
    assert (await services.memory.get("time")).value == "Torce pro Avaí"
    assert "Torce" not in provider.calls[0]["system"] and "eu torço pro Avaí" in provider.calls[0]["messages"][0].content
    session = await stm.session()
    assert session.turns == [] and [r.id for r in session.refs] == ["run1"]  # running ref kept


async def test_promotion_waits_when_budget_is_spent(services, stm):
    services.memory = MemoryService(InMemoryMemoryStore(), make_settings())
    provider, usage = with_llm(services)
    usage.records.append(
        UsageRecord(
            provider="p", model="m", input_tokens=0, output_tokens=0, purpose="t",
            cost_usd=services.settings.proactive_daily_budget_usd, scope=Scope.PROACTIVE,
        )
    )
    await stm.add_exchange("oi", "oi")
    result = await promote_idle_sessions(services, now=utcnow() + timedelta(hours=4))
    assert result["skipped"] == 1 and provider.calls == []
    assert (await stm.session()).turns  # kept for a later try


async def test_active_sessions_are_not_promoted(services, stm):
    with_llm(services)
    await stm.add_exchange("oi", "oi")
    assert (await promote_idle_sessions(services))["sessions"] == 0


async def test_task_is_done_before_the_user_is_notified(services, stm):
    services.search = {"web": FakeSearch("web", SearchResult("F1", "https://f1.com", "x"))}
    with_llm(services, "Resumo.")
    record = await services.tasks.enqueue("search", {"query": "F1"}, None)
    await stm.add_ref("pesquisa", record.id, "pesquisa: F1", "na fila")
    seen = []

    async def notify(reply):
        # What a follow-up question would see right when the message arrives.
        seen.append(services.tasks.store.records[record.id].status)

    await run_action_task(
        services, notify, task_id=record.id, action_name="search", params=record.params,
        trace_id=None, attempt=0, max_retries=3,
    )
    assert seen == [TaskStatus.DONE]


async def test_notices_keep_more_text(stm):
    await stm.add_turn("notice", "x" * 1500)
    await stm.add_turn("user", "y" * 1500)
    turns = (await stm.session()).turns
    assert len(turns[0].text) == 1500 and len(turns[1].text) == 500


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("how is that research going?", "pesquisa"),
        ("how's the task?", "tarefa"),
        ("did it work?", None),
        ("is it done?", None),
        ("what about that?", None),
    ],
)
def test_status_questions_in_english(text, kind):
    question = status_question(text)
    assert question is not None and question.kind == kind


async def test_status_reply_and_context_in_english(services, stm):
    from uva import i18n
    from uva.stm.service import describe_ref

    i18n.set_language("en")
    record = await services.tasks.enqueue("search", {"query": "Formula 1"}, None)
    await stm.add_ref("pesquisa", record.id, "research: Formula 1", "na fila")
    with_llm(services)

    reply = (await Router(services).handle_text("how is that research going?")).text

    assert reply.endswith(f"research: Formula 1 (t1, id {record.id}) is queued.")
    session = await stm.session()
    line = describe_ref(session.refs[-1], services.tz, focus=True)
    assert "[research]" in line and "⏳ queued" in line and line.endswith("← in focus")
    assert session.refs[-1].status == "na fila"  # stored value unchanged
