"""LLMClient: wraps a provider with cost accounting and event logging."""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from uva.config import LLMProvider as ProviderName
from uva.config import Settings
from uva.i18n import t
from uva.llm.base import LLMError, LLMMessage, LLMProvider, LLMResponse, without_context
from uva.llm.budget import Scope, UsageRecord, UsageStore
from uva.llm.pricing import WEB_SEARCH_COST_USD, PriceTable
from uva.logging import get_logger
from uva.logging.events import EventType, current_trace_id, emit
from uva.soul import Soul

log = get_logger(__name__)


class TokenMeter:
    """Counts tokens of every LLM call made inside a ``metered()`` block."""

    def __init__(self) -> None:
        self.input_tokens = 0
        self.output_tokens = 0
        self.cost_usd = 0.0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


_meter: ContextVar[TokenMeter | None] = ContextVar("uva_token_meter", default=None)


# Set by the dashboard's chat: every LLM call of the message is recorded here
# (prompt, context, raw answer, tokens, cost, time) for its debug panel.
_debug: ContextVar[list[dict] | None] = ContextVar("uva_llm_debug", default=None)


@contextmanager
def debug_calls() -> Iterator[list[dict]]:
    calls: list[dict] = []
    token = _debug.set(calls)
    try:
        yield calls
    finally:
        _debug.reset(token)


@contextmanager
def metered() -> Iterator[TokenMeter]:
    meter = TokenMeter()
    token = _meter.set(meter)
    try:
        yield meter
    finally:
        _meter.reset(token)


class LLMClient:
    def __init__(
        self,
        provider: LLMProvider,
        *,
        router_model: str,
        heavy_model: str,
        vision_model: str | None = None,
        prices: PriceTable,
        usage: UsageStore | None,
        max_tokens: int = 4000,
        soul: Soul | None = None,
    ) -> None:
        self.provider = provider
        self.router_model = router_model
        self.heavy_model = heavy_model
        self.vision_model = vision_model or router_model
        self.prices = prices
        self.usage = usage
        self.max_tokens = max_tokens
        self.soul = soul

    @property
    def provider_name(self) -> str:
        return self.provider.name

    @property
    def supports_web_search(self) -> bool:
        return hasattr(self.provider, "web_search")

    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        purpose: str,
        scope: Scope,
        model: str | None = None,
        system: str | None = None,
        json_schema: dict[str, Any] | None = None,
        max_tokens: int | None = None,
        use_soul: bool = True,
    ) -> LLMResponse:
        """One call. ``use_soul=False`` skips the personality (e.g. code generation)."""
        model = model or self.router_model
        if use_soul and self.soul is not None:
            system = self.soul.apply(system)
        started = time.perf_counter()
        try:
            response = await self.provider.complete(
                messages,
                model=model,
                system=system,
                json_schema=json_schema,
                max_tokens=max_tokens or self.max_tokens,
            )
        except LLMError as exc:
            await emit(EventType.ERROR, where="llm", purpose=purpose, model=model, error=str(exc))
            raise
        await self._account(response, purpose, scope, started, request=without_context(messages[-1].content))
        if (calls := _debug.get()) is not None:
            calls.append({
                "purpose": purpose,
                "scope": scope.value,
                "model": response.model,
                "system": system or "",
                "messages": [
                    {"role": m.role, "content": m.content, "images": len(m.images)} for m in messages
                ],
                "schema": json_schema is not None,
                "response": response.text,
                "input_tokens": response.input_tokens,
                "output_tokens": response.output_tokens,
                "cached_input_tokens": int(response.extra.get("cached_input_tokens", 0) or 0),
                "cost_usd": round(self.prices.cost(
                    response.model, response.input_tokens, response.output_tokens,
                    int(response.extra.get("cached_input_tokens", 0) or 0),
                ), 6),
                "duration_ms": round((time.perf_counter() - started) * 1000),
            })
        return response

    async def web_search(self, query: str, *, system: str, scope: Scope) -> LLMResponse:
        """Provider-side web search (Anthropic only)."""
        if not self.supports_web_search:
            raise LLMError(t("llm.client.no_web_search", provider=self.provider_name))
        if self.soul is not None:
            system = self.soul.apply(system) or system
        started = time.perf_counter()
        try:
            response = await self.provider.web_search(  # type: ignore[attr-defined]
                query, model=self.heavy_model, system=system
            )
        except LLMError as exc:
            await emit(EventType.ERROR, where="llm", purpose="web_search", error=str(exc))
            raise
        await self._account(response, "web_search", scope, started, request=query)
        return response

    async def _account(
        self, response: LLMResponse, purpose: str, scope: Scope, started: float, request: str
    ) -> None:
        if (meter := _meter.get()) is not None:
            meter.input_tokens += response.input_tokens
            meter.output_tokens += response.output_tokens
        cached = int(response.extra.get("cached_input_tokens", 0) or 0)
        cost = self.prices.cost(response.model, response.input_tokens, response.output_tokens, cached)
        cost += response.extra.get("web_search_requests", 0) * WEB_SEARCH_COST_USD
        if meter is not None:
            meter.cost_usd += cost
        duration_ms = round((time.perf_counter() - started) * 1000)

        if self.usage is not None:
            record = UsageRecord(
                provider=self.provider_name,
                model=response.model,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                cost_usd=cost,
                scope=scope,
                purpose=purpose,
                trace_id=current_trace_id(),
            )
            try:
                await self.usage.add(record)
            except Exception as exc:
                log.warning("usage_store_unavailable", error=str(exc))

        await emit(
            EventType.LLM_CALL,
            provider=self.provider_name,
            model=response.model,
            purpose=purpose,
            scope=scope.value,
            input_tokens=response.input_tokens,
            cached_input_tokens=cached,
            output_tokens=response.output_tokens,
            cost_usd=round(cost, 6),
            price_known=self.prices.knows(response.model),
            duration_ms=duration_ms,
            request=request,
            response=response.text,
        )


def build_llm_client(settings: Settings, usage: UsageStore | None) -> LLMClient | None:
    """LLMClient from settings, or None when the LLM is not configured."""
    if settings.llm_api_key is None or not settings.llm_api_key.get_secret_value().strip():
        log.info("llm_disabled", reason="LLM_API_KEY not set")
        return None
    if settings.router_model is None or settings.heavy_model is None:
        log.warning("llm_disabled", reason="LLM_MODEL_ROUTER not set")
        return None
    api_key = settings.llm_api_key.get_secret_value().strip()
    timeout = settings.llm_timeout_seconds

    provider: LLMProvider
    if settings.llm_provider is ProviderName.ANTHROPIC:
        from uva.llm.anthropic_provider import AnthropicProvider

        provider = AnthropicProvider(api_key, timeout=timeout)
    else:
        from uva.llm.openai_provider import OpenAICompatibleProvider

        provider = OpenAICompatibleProvider(
            api_key,
            settings.llm_base_url,
            reasoning_effort=settings.llm_reasoning_effort,
            timeout=timeout,
        )

    return LLMClient(
        provider,
        router_model=settings.router_model,
        heavy_model=settings.heavy_model,
        vision_model=settings.vision_model,
        prices=PriceTable(settings.llm_prices, settings.llm_default_price),
        usage=usage,
        max_tokens=settings.llm_max_tokens,
        soul=Soul(settings.soul_file, profile=settings.profile_file),
    )
