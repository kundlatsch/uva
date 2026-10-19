"""Fakes for the MCP tests: a gateway with a fixed catalog and an in-memory Redis."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fakes import make_services, make_settings

from uva.mcp.catalog import McpService
from uva.mcp.client import McpUnavailable

SCHEMA_TEXT = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}


def tool(server: str, name: str, description: str = "", schema: dict | None = None, **annotations) -> dict:
    return {
        "server": server,
        "name": name,
        "description": description or f"{name} tool",
        "input_schema": schema or SCHEMA_TEXT,
        "annotations": annotations,
        "hash": f"h-{server}-{name}-{description}",
    }


class FakeGateway:
    def __init__(self, tools: list[dict], results: dict[str, dict] | None = None) -> None:
        self.tools = tools
        self.results = results or {}
        self.ver = "v1"
        self.down = False
        self.calls: list[tuple[str, str, dict]] = []
        self.toggled: list[tuple[str, bool]] = []
        self.reloads = 0
        self.servers_list: list[dict] = []

    def _check(self) -> None:
        if self.down:
            raise McpUnavailable("ConnectError")

    async def version(self) -> str:
        self._check()
        return self.ver

    async def catalog(self) -> dict:
        self._check()
        return {"tools": self.tools, "version": self.ver}

    async def servers(self) -> dict:
        self._check()
        return {"servers": self.servers_list, "config_error": None, "version": self.ver}

    async def call(self, server: str, name: str, arguments: dict, timeout_s: float) -> dict:
        self._check()
        self.calls.append((server, name, arguments))
        return self.results.get(name) or {
            "content": [{"type": "text", "text": f"{name} ok: {json.dumps(arguments)}"}],
            "is_error": False,
            "duration_ms": 3,
        }

    async def reload(self) -> dict:
        self._check()
        self.reloads += 1
        return {"ok": True}

    async def restart(self, name: str) -> None:
        self._check()

    async def set_enabled(self, name: str, enabled: bool) -> None:
        self._check()
        self.toggled.append((name, enabled))


class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, **_):
        self.data[key] = str(value)

    async def incr(self, key):
        self.data[key] = str(int(self.data.get(key) or 0) + 1)
        return int(self.data[key])

    async def mget(self, *keys):
        return [self.data.get(k) for k in keys]


def write_mcp_json(repo: Path, servers: dict) -> None:
    (repo / "data").mkdir(parents=True, exist_ok=True)
    (repo / "data" / "mcp.json").write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


def mcp_services(tmp_path: Path, servers: dict, tools: list[dict], *, redis=None, **settings):
    """Services with an McpService over a FakeGateway (repo = tmp_path)."""
    write_mcp_json(tmp_path, servers)
    services = make_services(settings=make_settings(repo_path=str(tmp_path), **settings))
    gateway = FakeGateway(tools)
    services.mcp = McpService(services.settings, gateway, redis)
    return services, gateway
