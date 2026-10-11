import json
from types import SimpleNamespace

import httpx
import openai
import pytest
from fakes import FakeSearch, with_llm

from uva.llm.agent import BAD_PLAN
from uva.llm.anthropic_provider import AnthropicProvider
from uva.llm.base import LLMMessage
from uva.llm.budget import Scope, UsageRecord
from uva.llm.openai_provider import OpenAICompatibleProvider
from uva.llm.plan import PlanError, parse_plan
from uva.llm.pricing import PriceTable
from uva.logging.events import EventType
from uva.router import Router
from uva.search.base import SearchResult
from uva.search.wikipedia import WikipediaBackend


def plan(*actions: dict, reply: str = "") -> str:
    return json.dumps({"actions": list(actions), "reply": reply})


@pytest.fixture
def router(services) -> Router:
    return Router(services)


# --- agent -------------------------------------------------------------------


async def test_direct_answer_without_actions(router, services, event_store):
    provider, usage = with_llm(services, plan(reply="Paris."))
    reply = await router.handle_text("qual a capital da França")

    assert reply.text == "Paris."
    assert provider.calls[0]["model"] == "router-model"
    assert provider.calls[0]["schema"]["properties"].keys() >= {"actions", "reply"}
    llm_event = event_store.of_type(EventType.LLM_CALL)[0]
    # 100 input tokens at $1/M + 50 output tokens at $2/M
    assert llm_event.data["cost_usd"] == pytest.approx(0.0002)
    assert usage.records[0].scope == Scope.INTERACTIVE


async def test_wikipedia_search_returns_summary_with_sources(router, services, event_store):
    wiki = FakeSearch(
        "wikipedia",
        SearchResult("Fórmula 1", "https://pt.wikipedia.org/wiki/F%C3%B3rmula_1", "Categoria..."),
    )
    web = FakeSearch("web")
    services.search = {"wikipedia": wiki, "web": web}
    provider, _ = with_llm(
        services,
        plan(
            {"type": "search", "params": {"query": "Fórmula 1", "source": "wikipedia"}},
            reply="Vou pesquisar na Wikipédia.",
        ),
        "A Fórmula 1 é a principal categoria do automobilismo.",
    )

    reply = await router.handle_text("pesquisa pra mim sobre Fórmula 1 na Wikipédia")

    assert wiki.queries == ["Fórmula 1"] and web.queries == []
    assert "principal categoria" in reply.text
    assert "Fontes:" in reply.text and "wikipedia.org" in reply.text
    assert provider.calls[1]["model"] == "heavy-model"  # summary uses the heavy model

    trace_id = event_store.events[0].trace_id
    detail = (await router.handle_text(f"/log trace {trace_id}")).text
    assert '"type": "search"' in detail and '"source": "wikipedia"' in detail


async def test_invalid_json_executes_nothing(router, services, event_store):
    with_llm(services, "isso não é json")
    reply = await router.handle_text("faça algo")
    assert reply.text == BAD_PLAN
    assert event_store.of_type(EventType.ACTION) == []
    error = event_store.of_type(EventType.ERROR)[0]
    assert error.data["where"] == "plan" and error.data["raw"] == "isso não é json"


async def test_unknown_action_rejects_whole_plan(router, services, event_store):
    with_llm(
        services,
        plan(
            {"type": "reply", "params": {"text": "oi"}},
            {"type": "rm_rf", "params": {"path": "/"}},
        ),
    )
    reply = await router.handle_text("apaga tudo")
    assert reply.text == BAD_PLAN
    assert event_store.of_type(EventType.ACTION) == []


async def test_invalid_params_reject_plan(router, services, event_store):
    with_llm(services, plan({"type": "search", "params": {"source": "wikipedia"}}))
    assert (await router.handle_text("pesquisa")).text == BAD_PLAN


async def test_run_command_action(router, services):
    with_llm(services, plan({"type": "run_command", "params": {"command": "/ping"}}))
    assert (await router.handle_text("você está funcionando?")).text == "pong"


async def test_history_is_sent_to_the_llm(router, services):
    provider, _ = with_llm(services, plan(reply="Oi, Gustavo!"), plan(reply="Você é o Gustavo."))
    await router.handle_text("oi, eu sou o Gustavo")
    await router.handle_text("quem sou eu?")
    sent = provider.calls[1]["messages"]
    assert [m.role for m in sent] == ["user", "assistant", "user"]
    assert sent[0].content == "oi, eu sou o Gustavo"


async def test_daily_budget_blocks_llm(router, services, event_store):
    provider, usage = with_llm(services, plan(reply="nunca"))
    usage.records.append(
        UsageRecord(
            provider="fake", model="x", input_tokens=0, output_tokens=0,
            cost_usd=services.settings.llm_daily_budget_usd, scope=Scope.INTERACTIVE, purpose="t",
        )
    )
    reply = await router.handle_text("conta uma piada")
    assert "orçamento diário" in reply.text
    assert provider.calls == []
    assert event_store.of_type(EventType.BUDGET)[0].data["blocked"] is True
    # Commands keep working.
    assert (await router.handle_text("/ping")).text == "pong"


async def test_proactive_budget_is_separate(services):
    _, usage = with_llm(services)
    usage.records.append(
        UsageRecord(
            provider="fake", model="x", input_tokens=0, output_tokens=0,
            cost_usd=services.settings.proactive_daily_budget_usd, scope=Scope.PROACTIVE,
            purpose="goal",
        )
    )
    assert not (await services.budget.check(Scope.PROACTIVE)).allowed
    assert (await services.budget.check(Scope.INTERACTIVE)).allowed


async def test_llm_command(router, services):
    with_llm(services)
    text = (await router.handle_text("/llm")).text
    assert "router-model" in text and "heavy-model" in text
    assert "Gasto hoje" in text


def test_parse_plan_accepts_code_fence():
    assert parse_plan('```json\n{"actions": [], "reply": "oi"}\n```').reply == "oi"
    with pytest.raises(PlanError):
        parse_plan('{"actions": "nope"}')


def test_unknown_model_uses_default_price():
    prices = PriceTable({}, (5.0, 25.0))
    assert prices.cost("claude-haiku-5-5", 1_000_000, 0) == pytest.approx(0.10)
    assert prices.cost("mystery", 1_000_000, 1_000_000) == pytest.approx(30.0)
    assert not prices.knows("mystery")
    # Dated model names resolve to their base entry.
    assert prices.cost("claude-opus-5-5-20260401", 1_000_000, 0) == pytest.approx(4.0)


def test_reply_action_repeating_plan_reply_is_not_duplicated():
    from uva.actions.registry import ActionResult
    from uva.llm.agent import compose_reply

    assert compose_reply("Paris.", [ActionResult("Paris.")]) == "Paris."


# --- provider adapters ---------------------------------------------------------


def _bad_request() -> openai.BadRequestError:
    response = httpx.Response(400, request=httpx.Request("POST", "http://test"))
    return openai.BadRequestError("bad", response=response, body=None)


class FakeChatCompletions:
    def __init__(self, fail_first: bool = False) -> None:
        self.requests: list[dict] = []
        self.fail_first = fail_first

    async def create(self, **request):
        self.requests.append(request)
        if self.fail_first and len(self.requests) == 1:
            raise _bad_request()
        return SimpleNamespace(
            model="gpt-test",
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"reply": "ok"}'))],
            usage=SimpleNamespace(prompt_tokens=12, completion_tokens=3),
        )


async def test_openai_adapter_uses_json_schema_and_falls_back():
    completions = FakeChatCompletions(fail_first=True)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    provider = OpenAICompatibleProvider("k", None, client=client, reasoning_effort="low")

    result = await provider.complete(
        [LLMMessage("user", "oi")], model="gpt-test", system="sys", json_schema={"type": "object"}
    )

    first, second = completions.requests
    assert first["response_format"]["type"] == "json_schema"
    assert second["response_format"] == {"type": "json_object"}
    assert first["messages"][0] == {"role": "system", "content": "sys"}
    assert first["reasoning_effort"] == "low"
    assert (result.text, result.input_tokens, result.output_tokens) == ('{"reply": "ok"}', 12, 3)


class FakeAnthropicMessages:
    def __init__(self, response) -> None:
        self.response = response
        self.requests: list[dict] = []

    async def create(self, **request):
        self.requests.append(request)
        return self.response


def _anthropic_response(content, searches=0):
    return SimpleNamespace(
        model="claude-test",
        stop_reason="end_turn",
        content=content,
        usage=SimpleNamespace(
            input_tokens=20,
            output_tokens=10,
            server_tool_use=SimpleNamespace(web_search_requests=searches),
        ),
    )


async def test_anthropic_adapter_structured_output():
    messages = FakeAnthropicMessages(_anthropic_response([SimpleNamespace(type="text", text="{}")]))
    provider = AnthropicProvider("k", client=SimpleNamespace(messages=messages))
    result = await provider.complete(
        [LLMMessage("user", "oi")], model="claude-test", system="sys", json_schema={"type": "object"}
    )
    request = messages.requests[0]
    assert request["output_config"]["format"] == {"type": "json_schema", "schema": {"type": "object"}}
    assert request["system"] == "sys"
    assert (result.text, result.input_tokens) == ("{}", 20)


async def test_anthropic_web_search_collects_sources():
    content = [
        SimpleNamespace(
            type="web_search_tool_result",
            content=[SimpleNamespace(url="https://a.com", title="A")],
        ),
        SimpleNamespace(type="text", text="Resumo."),
    ]
    messages = FakeAnthropicMessages(_anthropic_response(content, searches=2))
    provider = AnthropicProvider("k", client=SimpleNamespace(messages=messages))
    result = await provider.web_search("x", model="claude-test", system="s")
    assert messages.requests[0]["tools"][0]["type"] == "web_search_20260209"
    assert result.extra == {"web_search_requests": 2, "sources": [{"title": "A", "url": "https://a.com"}]}


# --- search backends ---------------------------------------------------------------


async def test_wikipedia_backend():
    def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        if params.get("list") == "search":
            return httpx.Response(200, json={"query": {"search": [{"pageid": 7}]}})
        assert params["pageids"] == "7"
        return httpx.Response(
            200,
            json={
                "query": {
                    "pages": {
                        "7": {"title": "Fórmula 1", "fullurl": "https://pt.wikipedia.org/wiki/F1", "extract": "Texto"}
                    }
                }
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    response = await WikipediaBackend("pt", client=client).search("F1", 3)
    assert response.results == [SearchResult("Fórmula 1", "https://pt.wikipedia.org/wiki/F1", "Texto")]
