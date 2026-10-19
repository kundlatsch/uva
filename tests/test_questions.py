import json
from datetime import timedelta

import pytest
from fakes import FakeBusy, InMemoryGoalStore, with_llm

from uva.clock import utcnow
from uva.goals.runner import proactive_tick
from uva.goals.store import Goal, GoalKind, GoalStatus
from uva.questions import TTL, QuestionStore
from uva.router import Router
from uva.storage.sqlite import Database


@pytest.fixture
def qs(services, tmp_path):
    db = Database(tmp_path / "uva.db")
    db.migrate()
    services.sqlite = db
    return services


def plan(reply="", *actions) -> str:
    return json.dumps({"actions": [{"type": t, "params": p} for t, p in actions], "reply": reply})


ASK = ("ask", {"question": "Qual cidade?", "options": ["Lisboa", "Porto"], "context": "booking the hotel"})


async def test_ask_sends_buttons_and_the_choice_comes_back_as_his_turn(qs):
    provider, _ = with_llm(qs, plan("", ASK), plan("Ótimo, Lisboa então."))
    router = Router(qs)
    reply = await router.handle_text("reserva um hotel pra mim")
    assert "Qual cidade?" in reply.text
    assert [b.text for b in reply.buttons] == ["Lisboa", "Porto", "✏️ outra coisa"]
    data = reply.buttons[0].data
    assert data.startswith("ask:") and data.endswith(":0")
    answered = await router.handle_callback(data)
    assert answered.text == "Ótimo, Lisboa então."
    message = provider.calls[1]["messages"][-1].content
    assert message.endswith("Lisboa") and "Answer to your question: Qual cidade?" in message
    assert "booking the hotel" in message
    assert "já foi respondida" in (await router.handle_callback(data)).text  # once only


async def test_something_else_waits_for_the_typed_answer(qs):
    provider, _ = with_llm(qs, plan("", ASK), plan("Coimbra, anotado."))
    router = Router(qs)
    reply = await router.handle_text("reserva um hotel")
    other = reply.buttons[-1].data
    assert "escreve sua resposta para: Qual cidade?" in (await router.handle_callback(other)).text
    answered = await router.handle_text("Coimbra")
    assert answered.text == "Coimbra, anotado."
    assert "Answer to your question" in provider.calls[1]["messages"][-1].content


async def test_questions_expire(qs):
    store = QuestionStore(qs.sqlite)
    question = await store.create("x?", ["a", "b"], now=utcnow() - TTL - timedelta(minutes=1))
    reply = await Router(qs).handle_callback(f"ask:{question.id}:0")
    assert "expirou" in reply.text
    with pytest.raises(ValueError):
        await store.create("y?", ["só uma"])


async def test_a_blocked_goal_asks_with_options_and_resumes_with_the_answer(qs):
    qs.goals = InMemoryGoalStore()
    qs.busy = FakeBusy()
    goal = Goal(title="Escolher notebook", kind=GoalKind.UNICO, next_run_at=utcnow() - timedelta(minutes=1))
    await qs.goals.add(goal)
    run = json.dumps({"actions": [], "reply": "Achei dois modelos.", "done": True, "needs_feature": None})
    verdict = json.dumps({"verdict": "blocked", "reason": "Qual orçamento?", "options": ["até R$ 4 mil", "até R$ 6 mil"]})
    second = json.dumps({"actions": [], "reply": "Fico com o de 4 mil.", "done": True, "needs_feature": None})
    provider, _ = with_llm(qs, run, verdict, second, json.dumps({"verdict": "done", "reason": "Escolhido."}))
    sent = []

    async def notify(reply):
        sent.append(reply)

    await proactive_tick(qs, notify=notify)
    stored = qs.goals.goals[goal.id]
    assert stored.status is GoalStatus.PAUSADO and stored.question_id is not None
    report = sent[0]
    assert "Escolhe uma resposta" in report.text and [b.text for b in report.buttons][:2] == ["até R$ 4 mil", "até R$ 6 mil"]

    reply = await Router(qs).handle_callback(report.buttons[0].data)
    assert "voltei ao objetivo “Escolher notebook”" in reply.text
    stored = qs.goals.goals[goal.id]
    assert stored.status is GoalStatus.ATIVO and stored.answer == "Qual orçamento? -> até R$ 4 mil"

    await proactive_tick(qs, utcnow() + timedelta(minutes=30), notify=notify)  # past the run spacing
    brief = provider.calls[2]["messages"][0].content
    assert "He answered your question (question -> answer): Qual orçamento? -> até R$ 4 mil" in brief
    assert qs.goals.goals[goal.id].answer == ""  # used once
