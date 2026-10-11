"""The JSON contract between the LLM and the bot.

The LLM must answer with::

    {"actions": [{"type": "search", "params": {...}}], "reply": "..."}

Anything that doesn't parse or validate is rejected as a whole: no action
from a broken answer is executed.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, Field, ValidationError


class ActionCall(BaseModel):
    type: str = Field(description="Nome de uma das ações disponíveis")
    params: dict[str, Any] = Field(default_factory=dict)


class LLMPlan(BaseModel):
    actions: list[ActionCall] = Field(default_factory=list)
    reply: str = Field(default="", description="Texto enviado ao usuário")


PLAN_SCHEMA: dict[str, Any] = LLMPlan.model_json_schema()


class PlanError(ValueError):
    pass


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def parse_plan(text: str) -> LLMPlan:
    """Parse and validate the LLM answer, tolerating a ```json fence."""
    cleaned = _FENCE.sub("", text.strip())
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise PlanError(f"JSON inválido: {exc}") from exc
    try:
        return LLMPlan.model_validate(data)
    except ValidationError as exc:
        raise PlanError(f"formato inválido: {exc}") from exc
