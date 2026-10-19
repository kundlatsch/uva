"""The bot/worker side of the MCP gateway: plain HTTP calls (Celery-friendly).

When the gateway is down, MCP actions answer a friendly error and everything
else keeps working (same rule as "memory never blocks an answer").
"""

from __future__ import annotations

from typing import Any

import httpx

from uva.config import Settings


class McpUnavailable(RuntimeError):
    """The gateway can't be reached, refused the token or failed."""


class McpCallError(RuntimeError):
    """The gateway answered, but the call can't be made (unknown tool, timeout...)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class McpGateway:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.url = settings.mcp_url
        token = settings.mcp_gateway_token
        self.token = token.get_secret_value() if token is not None else ""
        self.transport = transport

    def _client(self, timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.url,
            timeout=timeout,
            transport=self.transport,
            headers={"Authorization": f"Bearer {self.token}"},
        )

    async def _request(self, method: str, path: str, payload: dict | None = None, timeout: float = 10) -> dict:
        try:
            async with self._client(timeout) as client:
                response = await client.request(method, path, json=payload)
        except httpx.HTTPError as exc:
            raise McpUnavailable(exc.__class__.__name__) from exc
        try:
            data = response.json()
        except ValueError:
            data = {}
        if response.status_code == 401:
            raise McpUnavailable("unauthorized (MCP_GATEWAY_TOKEN)")
        if response.status_code in (404, 403, 409, 422, 502, 503, 504):
            raise McpCallError(response.status_code, str(data.get("error") or data.get("detail") or response.status_code))
        if response.status_code != 200:
            raise McpUnavailable(f"HTTP {response.status_code}")
        return data

    async def servers(self) -> dict[str, Any]:
        return await self._request("GET", "/servers")

    async def catalog(self) -> dict[str, Any]:
        return await self._request("GET", "/catalog")

    async def version(self) -> str:
        return str((await self._request("GET", "/version", timeout=3)).get("version", ""))

    async def call(self, server: str, tool: str, arguments: dict[str, Any], timeout_s: float) -> dict[str, Any]:
        payload = {"server": server, "tool": tool, "arguments": arguments, "timeout_s": timeout_s}
        return await self._request("POST", "/call", payload, timeout=timeout_s + 15)

    async def reload(self) -> dict[str, Any]:
        return await self._request("POST", "/reload", timeout=30)

    async def restart(self, name: str) -> None:
        await self._request("POST", f"/servers/{name}/restart", timeout=30)

    async def set_enabled(self, name: str, enabled: bool) -> None:
        await self._request("POST", f"/servers/{name}/enabled", {"enabled": enabled}, timeout=30)
