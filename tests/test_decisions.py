import base64
import json
from types import SimpleNamespace

import httpx
import pytest
from fakes import InMemoryUsageStore, make_settings, with_llm
from pydantic import SecretStr, ValidationError

from uva.commands.decide import parse
from uva.llm.budget import Budget, Scope, UsageRecord
from uva.llm.decisions import DecisionsClient, Question, build_decisions_client
from uva.media import to_data_url
from uva.router import Router


class FakeDecisions:
    def __init__(self, answers, input_tokens=1000):
        self.answers = answers
        self.input_tokens = input_tokens
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        usage = SimpleNamespace(input_tokens=self.input_tokens)
        return SimpleNamespace(answers=self.answers, usage=usage)


def client_with(answers, usage=None, tokens=1000):
    fake = FakeDecisions(answers, tokens)
    client = DecisionsClient("k", model="gpt-6-luna", usage=usage, client=SimpleNamespace(decisions=fake))
    return client, fake


@pytest.fixture
def decide_services(services):
    usage = InMemoryUsageStore()
    services.budget = Budget(usage, services.settings, services.tz)
    return services, usage


def prob(value, p, label=None):
    return SimpleNamespace(value=value, label=label, probability=p)


PREDICATE = [SimpleNamespace(type="predicate", name="afirmacao", probability=0.873)]
CHOICE = [
    SimpleNamespace(
        type="choice", name="escolha", choice="sushi", confidence=0.71,
        probabilities=[prob("pizza", 0.2), prob("sushi", 0.71), prob("salada", 0.09)],
    )
]
SCORE = [
    SimpleNamespace(
        type="score", name="nota", score=1.8, confidence=0.6,
        probabilities=[prob(0, 0.1, "baixa"), prob(1, 0.2, "média"), prob(2, 0.7, "alta")],
    )
]


async def test_predicate_command_and_cost(decide_services):
    services, usage = decide_services
    services.decisions, fake = client_with(PREDICATE, usage, tokens=2000)
    reply = (await Router(services).handle_text("/decidir esse email é spam: Ganhe um iPhone grátis")).text
    assert "87% de chance de ser verdade" in reply
    call = fake.calls[0]
    assert call["model"] == "gpt-6-luna" and call["input"] == "Ganhe um iPhone grátis"
    assert call["questions"] == [
        {"type": "predicate", "name": "afirmacao", "instructions": "Isto é verdade? esse email é spam"}
    ]
    record = usage.records[0]
    assert record.purpose == "decisions" and record.cost_usd == pytest.approx(2000 * 0.10 / 1e6)


async def test_choice_command(decide_services):
    services, _ = decide_services
    services.decisions, fake = client_with(CHOICE)
    text = "/decidir o que jantar | pizza, sushi, salada :: tô sem grana"
    reply = (await Router(services).handle_text(text)).text
    assert "Escolha: sushi (confiança 71%)" in reply
    assert reply.index("- sushi: 71%") < reply.index("- pizza: 20%")  # best first
    question = fake.calls[0]["questions"][0]
    assert question["type"] == "choice"
    assert [c["value"] for c in question["choices"]] == ["pizza", "sushi", "salada"]
    assert fake.calls[0]["input"] == "tô sem grana"


async def test_score_command(decide_services):
    services, _ = decide_services
    services.decisions, fake = client_with(SCORE)
    text = "/decidir nota urgência | baixa, média, alta :: o servidor caiu"
    reply = (await Router(services).handle_text(text)).text
    assert "Nota 1,80 numa escala de 0 a 2 (≈ alta, confiança 60%)" in reply
    levels = fake.calls[0]["questions"][0]["levels"]
    assert levels == [{"label": "baixa"}, {"label": "média"}, {"label": "alta"}]


async def test_refusal_is_reported(decide_services):
    services, _ = decide_services
    services.decisions, _ = client_with([SimpleNamespace(type="refusal", name="afirmacao")])
    reply = (await Router(services).handle_text("/decidir isso é perigoso")).text
    assert "se recusou" in reply


async def test_photo_caption_sends_inline_image(decide_services):
    services, _ = decide_services
    services.decisions, fake = client_with(PREDICATE)
    photo = to_data_url(b"\xff\xd8fakejpeg")
    await Router(services).handle_text("/decidir tem um gato na foto", photo=photo)
    message = fake.calls[0]["input"][0]
    assert message["role"] == "user"
    assert message["content"][1] == {"type": "input_image", "image_url": photo}
    assert photo.startswith("data:image/jpeg;base64,")
    assert base64.b64decode(photo.split(",", 1)[1]) == b"\xff\xd8fakejpeg"
    assert services.last_photo.get() == photo


async def test_llm_action_uses_last_photo(decide_services):
    services, _ = decide_services
    services.decisions, fake = client_with([SimpleNamespace(type="predicate", name="gato", probability=0.9)])
    services.last_photo.set("data:image/png;base64,AAAA")
    question = {"type": "predicate", "name": "gato", "instructions": "Há um gato?"}
    plan = {
        "actions": [{"type": "decide", "params": {"use_last_photo": True, "questions": [question]}}],
        "reply": "Vou ver.",
    }
    with_llm(services, json.dumps(plan))
    reply = await Router(services).handle_text("tem gato nessa foto?")
    assert reply.parts[-1].startswith("📊 Há um gato?")
    assert fake.calls[0]["input"][0]["content"][1]["image_url"] == "data:image/png;base64,AAAA"


async def test_budget_blocks_command(decide_services):
    services, usage = decide_services
    services.decisions, fake = client_with(PREDICATE)
    usage.records.append(
        UsageRecord(
            provider="p", model="m", input_tokens=0, output_tokens=0, purpose="t",
            cost_usd=services.settings.llm_daily_budget_usd, scope=Scope.INTERACTIVE,
        )
    )
    reply = (await Router(services).handle_text("/decidir chove hoje")).text
    assert "orçamento diário" in reply and fake.calls == []


async def test_missing_key_is_reported(services):
    services.decisions = None
    assert "não está configurada" in (await Router(services).handle_text("/decidir x")).text


def test_key_resolution():
    anthropic = make_settings(llm_provider="anthropic", llm_api_key="sk-ant")
    assert build_decisions_client(anthropic, None) is None
    openai_main = make_settings(
        llm_provider="openai_compatible", llm_api_key="sk-openai", llm_base_url="https://api.openai.com/v1"
    )
    assert build_decisions_client(openai_main, None).api_key == "sk-openai"
    ollama = make_settings(
        llm_provider="openai_compatible", llm_api_key="x", llm_base_url="http://localhost:11434/v1"
    )
    assert build_decisions_client(ollama, None) is None
    explicit = make_settings(decisions_api_key=SecretStr("sk-dec"))
    assert build_decisions_client(explicit, None).api_key == "sk-dec"


async def test_http_fallback_without_sdk_support():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert request.url.path.endswith("/decisions") and body["model"] == "gpt-6-luna"
        assert request.headers["authorization"] == "Bearer k"
        answers = [{"type": "predicate", "name": "x", "probability": 0.5}]
        return httpx.Response(200, json={"answers": answers, "usage": {"input_tokens": 10}})

    client = DecisionsClient(
        "k", model="gpt-6-luna", client=SimpleNamespace(), http_transport=httpx.MockTransport(handler)
    )
    answers = await client.decide("texto", [Question(type="predicate", name="x", instructions="é verdade?")])
    assert answers[0].probability == 0.5


@pytest.mark.parametrize(
    "bad",
    [
        {"type": "choice", "name": "c", "instructions": "qual?", "choices": [{"value": "uma"}]},
        {"type": "score", "name": "s", "instructions": "nota?", "levels": [{"label": "um"}]},
        {"type": "outro", "name": "x", "instructions": "abc"},
    ],
)
def test_invalid_questions_are_rejected(bad):
    with pytest.raises(ValidationError):
        Question.model_validate(bad)


def test_parse_usage_errors():
    for text in ["", "o que | uma", "nota urgência | baixa, alta"]:
        with pytest.raises(ValueError):
            parse(text)
    evidence, questions = parse("nota beleza | feio, ok, lindo", has_photo=True)
    assert questions[0].type == "score" and evidence == ""
