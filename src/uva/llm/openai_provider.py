"""Adapter for OpenAI and OpenAI-compatible APIs (Chat Completions)."""

from __future__ import annotations

from typing import Any

import openai

from uva.llm.base import LLMError, LLMMessage, LLMResponse


class OpenAICompatibleProvider:
    name = "openai_compatible"

    def __init__(
        self,
        api_key: str | None,
        base_url: str | None,
        *,
        reasoning_effort: str | None = None,
        client: Any = None,
        timeout: float = 60.0,
    ) -> None:
        self.client = client or openai.AsyncOpenAI(
            api_key=api_key or "not-needed", base_url=base_url or None, timeout=timeout
        )
        self.reasoning_effort = reasoning_effort
        # Some compatible servers only know {"type": "json_object"}.
        self._json_mode = "json_schema"

    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        model: str,
        system: str | None = None,
        json_schema: dict[str, Any] | None = None,
        max_tokens: int = 4000,
    ) -> LLMResponse:
        chat: list[dict[str, str]] = []
        if system:
            chat.append({"role": "system", "content": system})
        chat.extend({"role": m.role, "content": _content(m)} for m in messages)

        request: dict[str, Any] = {
            "model": model,
            "messages": chat,
            "max_completion_tokens": max_tokens,
        }
        if self.reasoning_effort:
            request["reasoning_effort"] = self.reasoning_effort

        if json_schema is not None:
            request["response_format"] = self._response_format(json_schema)

        try:
            response = await self.client.chat.completions.create(**request)
        except openai.BadRequestError as exc:
            if self._json_mode != "json_schema" or json_schema is None:
                raise LLMError(f"openai: {exc}") from exc
            # Fall back to plain JSON mode once and remember it.
            self._json_mode = "json_object"
            request["response_format"] = self._response_format(json_schema)
            try:
                response = await self.client.chat.completions.create(**request)
            except openai.APIError as retry_exc:
                raise LLMError(f"openai: {retry_exc}") from retry_exc
        except openai.APIError as exc:
            raise LLMError(f"openai: {exc}") from exc

        choice = response.choices[0]
        usage = response.usage
        return LLMResponse(
            text=choice.message.content or "",
            model=response.model or model,
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
        )

    def _response_format(self, json_schema: dict[str, Any]) -> dict[str, Any]:
        if self._json_mode == "json_object":
            return {"type": "json_object"}
        return {
            "type": "json_schema",
            "json_schema": {"name": "uva_response", "schema": json_schema, "strict": False},
        }


def _content(message: LLMMessage) -> Any:
    """Plain text, or text + image_url parts when the message has images."""
    if not message.images:
        return message.content
    parts: list[dict[str, Any]] = [{"type": "text", "text": message.content}]
    parts += [{"type": "image_url", "image_url": {"url": url}} for url in message.images]
    return parts
