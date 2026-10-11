"""Anthropic (Claude) adapter."""

from __future__ import annotations

from typing import Any

import anthropic

from uva.llm.base import LLMError, LLMMessage, LLMResponse

WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search"}


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str | None, client: Any = None, timeout: float = 60.0) -> None:
        self.client = client or anthropic.AsyncAnthropic(api_key=api_key, timeout=timeout)
        # Flipped off if the API rejects our schema; then we rely on the prompt
        # asking for JSON and validate afterwards.
        self._native_json = True

    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        model: str,
        system: str | None = None,
        json_schema: dict[str, Any] | None = None,
        max_tokens: int = 4000,
    ) -> LLMResponse:
        request: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
        }
        if system:
            request["system"] = system
        if json_schema is not None and self._native_json:
            request["output_config"] = {"format": {"type": "json_schema", "schema": json_schema}}

        try:
            response = await self.client.messages.create(**request)
        except anthropic.BadRequestError as exc:
            if "output_config" not in request:
                raise LLMError(f"anthropic bad request: {exc}") from exc
            # Schema not accepted: retry once without native structured output.
            self._native_json = False
            request.pop("output_config")
            try:
                response = await self.client.messages.create(**request)
            except anthropic.APIError as retry_exc:
                raise LLMError(f"anthropic: {retry_exc}") from retry_exc
        except anthropic.APIError as exc:
            raise LLMError(f"anthropic: {exc}") from exc

        return _to_response(response)

    async def web_search(
        self, query: str, *, model: str, system: str, max_uses: int = 3, max_tokens: int = 4000
    ) -> LLMResponse:
        """Let Claude search the web (server tool) and answer with sources."""
        try:
            response = await self.client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                tools=[{**WEB_SEARCH_TOOL, "max_uses": max_uses}],
                messages=[{"role": "user", "content": query}],
            )
        except anthropic.APIError as exc:
            raise LLMError(f"anthropic web search: {exc}") from exc

        result = _to_response(response)
        result.extra["sources"] = _sources(response)
        return result


def _to_response(response: Any) -> LLMResponse:
    if getattr(response, "stop_reason", None) == "refusal":
        raise LLMError("anthropic: request refused")
    text = "".join(b.text for b in response.content if getattr(b, "type", None) == "text")
    usage = response.usage
    server_tools = getattr(usage, "server_tool_use", None)
    searches = getattr(server_tools, "web_search_requests", 0) or 0
    return LLMResponse(
        text=text,
        model=response.model,
        input_tokens=usage.input_tokens or 0,
        output_tokens=usage.output_tokens or 0,
        extra={"web_search_requests": searches} if searches else {},
    )


def _sources(response: Any) -> list[dict[str, str]]:
    """URLs/titles from web search result blocks, de-duplicated."""
    seen: dict[str, str] = {}
    for block in response.content:
        if getattr(block, "type", None) != "web_search_tool_result":
            continue
        content = block.content
        if not isinstance(content, list):  # error object
            continue
        for item in content:
            url = getattr(item, "url", None)
            if url and url not in seen:
                seen[url] = getattr(item, "title", "") or url
    return [{"title": title, "url": url} for url, title in seen.items()]
