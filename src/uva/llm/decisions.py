"""OpenAI Decisions API (public beta): typed answers instead of free text.

POST /v1/decisions evaluates shared evidence (text and/or inline images)
against independent questions:
  * predicate -> probability (0..1) that a condition is true;
  * choice    -> one of fixed options, with probabilities and confidence;
  * score     -> probability-weighted index over ordered levels (from 0).
Any answer can come back as a "refusal". Only model gpt-6-luna is supported;
input costs $0.10 per 1M tokens and output is free.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

import httpx
import openai
from pydantic import BaseModel, Field, model_validator

from uva.config import LLMProvider, Settings
from uva.i18n import t
from uva.llm.budget import Scope, UsageRecord, UsageStore
from uva.logging import get_logger
from uva.logging.events import EventType, current_trace_id, emit

log = get_logger(__name__)

OPENAI_BASE_URL = "https://api.openai.com/v1"
MAX_QUESTIONS = 8


# --- request models (validated before anything is sent) -------------------------------


class Choice(BaseModel):
    value: str = Field(min_length=1)
    description: str = ""


class Level(BaseModel):
    label: str = Field(min_length=1)
    description: str = ""


class Question(BaseModel):
    type: Literal["predicate", "choice", "score"]
    name: str = Field(min_length=1, max_length=64, description="Identificador único da pergunta")
    instructions: str = Field(min_length=3, description="O que avaliar")
    choices: list[Choice] | None = Field(default=None, description="Para choice: opções fixas")
    levels: list[Level] | None = Field(
        default=None, description="Para score: níveis em ordem, do mais baixo ao mais alto"
    )

    @model_validator(mode="after")
    def _shape(self) -> Question:
        if self.type == "choice" and (not self.choices or len(self.choices) < 2):
            raise ValueError(t("llm.decisions.errors.choices"))
        if self.type == "score" and (not self.levels or len(self.levels) < 2):
            raise ValueError(t("llm.decisions.errors.levels"))
        return self

    def to_api(self) -> dict[str, Any]:
        data: dict[str, Any] = {"type": self.type, "name": self.name, "instructions": self.instructions}
        if self.type == "choice":
            data["choices"] = [c.model_dump(exclude_defaults=True) for c in self.choices or []]
        if self.type == "score":
            data["levels"] = [lv.model_dump(exclude_defaults=True) for lv in self.levels or []]
        return data


def validate_questions(questions: list[Question]) -> None:
    if not questions:
        raise ValueError(t("llm.decisions.errors.none"))
    if len(questions) > MAX_QUESTIONS:
        raise ValueError(t("llm.decisions.errors.too_many", max=MAX_QUESTIONS))
    names = [q.name for q in questions]
    if len(set(names)) != len(names):
        raise ValueError(t("llm.decisions.errors.unique"))


# --- answers -----------------------------------------------------------------------------


@dataclass(slots=True)
class Answer:
    name: str
    type: str  # predicate | choice | score | refusal
    probability: float | None = None
    choice: str | None = None
    score: float | None = None
    confidence: float | None = None
    # (value or label, probability), best first
    probabilities: list[tuple[str, float]] = field(default_factory=list)


class DecisionsError(RuntimeError):
    pass


def parse_answers(raw: list[Any]) -> list[Answer]:
    """SDK objects or plain dicts -> Answer list."""
    answers = []
    for item in raw:
        get = (lambda k, item=item: item.get(k)) if isinstance(item, dict) else (
            lambda k, item=item: getattr(item, k, None)
        )
        probs = []
        for p in get("probabilities") or []:
            pget = p.get if isinstance(p, dict) else (lambda k, p=p: getattr(p, k, None))
            label = pget("label") or pget("value")
            probs.append((str(label), float(pget("probability") or 0)))
        probs.sort(key=lambda pair: -pair[1])
        choice = get("choice")
        answers.append(
            Answer(
                name=str(get("name") or ""),
                type=str(get("type")),
                probability=get("probability"),
                choice=None if choice is None else str(choice),
                score=get("score"),
                confidence=get("confidence"),
                probabilities=probs,
            )
        )
    return answers


# --- client ------------------------------------------------------------------------------


class DecisionsClient:
    def __init__(
        self,
        api_key: str,
        *,
        model: str,
        base_url: str = OPENAI_BASE_URL,
        price_input_per_m: float = 0.10,
        usage: UsageStore | None = None,
        client: Any = None,
        timeout: float = 60,
        http_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.price_input_per_m = price_input_per_m
        self.usage = usage
        self.api_key = api_key
        self.timeout = timeout
        self.http_transport = http_transport
        self.client = client or openai.AsyncOpenAI(api_key=api_key, base_url=self.base_url, timeout=timeout)

    async def decide(
        self,
        evidence: str,
        questions: list[Question],
        *,
        image_data_url: str | None = None,
        scope: Scope = Scope.INTERACTIVE,
    ) -> list[Answer]:
        validate_questions(questions)
        payload_input: Any = evidence
        if image_data_url:
            payload_input = [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": evidence or "Avalie a imagem."},
                        {"type": "input_image", "image_url": image_data_url},
                    ],
                }
            ]
        api_questions = [q.to_api() for q in questions]

        started = time.perf_counter()
        try:
            if hasattr(self.client, "decisions"):
                response = await self.client.decisions.create(
                    model=self.model, input=payload_input, questions=api_questions
                )
                raw_answers, usage = response.answers, getattr(response, "usage", None)
                input_tokens = getattr(usage, "input_tokens", 0) or 0
            else:  # older SDK: plain HTTP
                data = await self._post(payload_input, api_questions)
                raw_answers = data.get("answers", [])
                input_tokens = (data.get("usage") or {}).get("input_tokens", 0) or 0
        except (openai.APIError, httpx.HTTPError) as exc:
            await emit(EventType.ERROR, where="decisions", error=str(exc))
            raise DecisionsError(str(exc)) from exc

        answers = parse_answers(list(raw_answers))
        cost = input_tokens * self.price_input_per_m / 1_000_000
        await self._account(input_tokens, cost, scope, started, evidence, answers)
        return answers

    async def _post(self, payload_input: Any, questions: list[dict]) -> dict:
        async with httpx.AsyncClient(timeout=self.timeout, transport=self.http_transport) as http:
            response = await http.post(
                f"{self.base_url}/decisions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"model": self.model, "input": payload_input, "questions": questions},
            )
            response.raise_for_status()
            return response.json()

    async def _account(self, input_tokens, cost, scope, started, evidence, answers) -> None:
        if self.usage is not None:
            try:
                await self.usage.add(
                    UsageRecord(
                        provider="openai", model=self.model, input_tokens=input_tokens,
                        output_tokens=0, cost_usd=cost, scope=scope, purpose="decisions",
                        trace_id=current_trace_id(),
                    )
                )
            except Exception as exc:
                log.warning("usage_store_unavailable", error=str(exc))
        await emit(
            EventType.LLM_CALL,
            provider="openai",
            model=self.model,
            kind="decisions",
            purpose="decisions",
            scope=scope.value,
            input_tokens=input_tokens,
            output_tokens=0,
            cost_usd=round(cost, 6),
            price_known=True,
            duration_ms=round((time.perf_counter() - started) * 1000),
            request=evidence,
            response=[asdict(a) for a in answers],
        )


def build_decisions_client(settings: Settings, usage: UsageStore | None) -> DecisionsClient | None:
    """None when there is no OpenAI key to use (the feature then says so)."""
    key = settings.decisions_api_key.get_secret_value().strip() if settings.decisions_api_key else ""
    base = settings.decisions_base_url or OPENAI_BASE_URL
    if not key and settings.llm_provider is LLMProvider.OPENAI_COMPATIBLE and settings.llm_api_key:
        llm_base = settings.llm_base_url or OPENAI_BASE_URL
        if "api.openai.com" in llm_base:  # the main key is an OpenAI key
            key = settings.llm_api_key.get_secret_value().strip()
    if not key:
        return None
    return DecisionsClient(
        key,
        model=settings.decisions_model,
        base_url=base,
        price_input_per_m=settings.decisions_price_input,
        usage=usage,
        timeout=settings.llm_timeout_seconds,
    )


# --- formatting --------------------------------------------------------------------------


def pct(value: float | None) -> str:
    return "?" if value is None else f"{round(value * 100)}%"


def format_answer(answer: Answer, question: Question | None) -> str:
    title = question.instructions if question else answer.name
    if answer.type == "refusal":
        return t("llm.decisions.refusal", title=title)
    if answer.type == "predicate":
        return t("llm.decisions.predicate", title=title, pct=pct(answer.probability))
    if answer.type == "choice":
        lines = [f"🎯 {title}", t("llm.decisions.choice", choice=answer.choice, confidence=pct(answer.confidence))]
        lines += [f"- {label}: {pct(p)}" for label, p in answer.probabilities[:5]]
        return "\n".join(lines)
    if answer.type == "score":
        levels = question.levels if question and question.levels else []
        top = len(levels) - 1
        nearest = levels[min(top, max(0, round(answer.score or 0)))].label if levels else "?"
        score = f"{answer.score:.2f}".replace(".", ",") if answer.score is not None else "?"
        lines = [
            f"📏 {title}",
            t("llm.decisions.score", score=score, top=top, nearest=nearest, confidence=pct(answer.confidence)),
        ]
        lines += [f"- {label}: {pct(p)}" for label, p in answer.probabilities[:5]]
        return "\n".join(lines)
    return t("llm.decisions.unknown", title=title, type=answer.type)


def format_answers(answers: list[Answer], questions: list[Question]) -> str:
    by_name = {q.name: q for q in questions}
    return "\n\n".join(format_answer(a, by_name.get(a.name)) for a in answers) or t("llm.decisions.no_answers")
