"""data/mcp.json: which MCP servers exist and UVA's policy for their tools.

Same ``mcpServers`` shape as Claude Desktop / Claude Code (so a config can be
copied from a server's README), plus a ``uva`` block per server. No secrets in
the file: ``${VAR}`` is expanded from the gateway's own environment (mcp.env)
inside the gateway only. The bot reads the same file for the policy, never the
secrets. Only Gustavo edits it (the bot's self-edit refuses it).
"""

from __future__ import annotations

import fnmatch
import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

DEFAULT_TIMEOUT_S = 30.0
DEFAULT_MAX_RESULT_CHARS = 6000
SERVER_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class McpConfigError(ValueError):
    pass


class Policy(BaseModel):
    """The ``uva`` block. Defaults are the safe ones: every tool allowed but
    treated as a write that needs a "yes", nothing proactive."""

    enabled: bool = True
    description: str = ""
    allow_tools: list[str] = Field(default_factory=lambda: ["*"])
    deny_tools: list[str] = Field(default_factory=list)
    # Reads: their result goes back to the LLM in the same turn (lookup).
    read_tools: list[str] = Field(default_factory=list)
    # Writes that need a confirmation button; [] = server trusted. Also overrides
    # the server's readOnlyHint (annotations are hints, not guarantees).
    confirm_tools: list[str] = Field(default_factory=lambda: ["*"])
    # Reads allowed in goals/initiatives.
    proactive_tools: list[str] = Field(default_factory=list)
    # Slow reads that run as background tasks.
    long_running_tools: list[str] = Field(default_factory=list)
    timeout_s: float = Field(DEFAULT_TIMEOUT_S, gt=0, le=600)
    max_result_chars: int = Field(DEFAULT_MAX_RESULT_CHARS, ge=200, le=50_000)
    # Calls running at once on this server (gateway).
    max_concurrency: int = Field(4, ge=1, le=32)
    # Word -> tool (phase 6, tiers 1 and 2).
    aliases: dict[str, str] = Field(default_factory=dict)

    def allows(self, tool: str) -> bool:
        return matches(tool, self.allow_tools) and not matches(tool, self.deny_tools)

    def is_read(self, tool: str, annotations: Mapping[str, Any] | None = None) -> bool:
        if matches(tool, self.read_tools):
            return True
        hint = bool((annotations or {}).get("readOnlyHint"))
        return hint and not matches(tool, self.confirm_tools)

    def needs_confirmation(self, tool: str) -> bool:
        return matches(tool, self.confirm_tools)


class ServerSpec(BaseModel):
    name: str
    type: Literal["stdio", "http"]
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    policy: Policy = Field(default_factory=Policy)

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        if not SERVER_NAME.match(value):
            raise ValueError("server names are lowercase letters, digits, '-' and '_' (max 32)")
        return value

    def check(self) -> None:
        if self.type == "stdio" and not self.command:
            raise McpConfigError(f"{self.name}: a stdio server needs 'command'")
        if self.type == "http" and not (self.url or "").startswith(("http://", "https://")):
            raise McpConfigError(f"{self.name}: an http server needs an http(s) 'url'")


def matches(name: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)


def parse_config(data: Mapping[str, Any]) -> dict[str, ServerSpec]:
    servers = data.get("mcpServers", {})
    if not isinstance(servers, Mapping):
        raise McpConfigError("'mcpServers' must be an object")
    specs: dict[str, ServerSpec] = {}
    for name, raw in servers.items():
        if not isinstance(raw, Mapping):
            raise McpConfigError(f"{name}: must be an object")
        raw = dict(raw)
        kind = raw.pop("type", None) or ("http" if raw.get("url") else "stdio")
        if kind in ("streamable-http", "streamable_http"):
            kind = "http"
        policy = raw.pop("uva", None) or {}
        try:
            spec = ServerSpec(name=name, type=kind, policy=Policy.model_validate(policy), **_known(raw))
        except ValidationError as exc:
            raise McpConfigError(f"{name}: {exc}") from exc
        spec.check()
        specs[name] = spec
    return specs


def _known(raw: dict[str, Any]) -> dict[str, Any]:
    keys = {"command", "args", "env", "cwd", "url", "headers"}
    return {k: v for k, v in raw.items() if k in keys}


def load_config(path: Path) -> dict[str, ServerSpec]:
    """The servers in ``path`` ({} when the file does not exist)."""
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise McpConfigError(f"{path.name}: invalid JSON ({exc})") from exc
    if not isinstance(data, Mapping):
        raise McpConfigError(f"{path.name}: must be a JSON object")
    return parse_config(data)


def referenced_vars(spec: ServerSpec) -> set[str]:
    texts = [spec.command or "", spec.url or "", spec.cwd or "", *spec.args, *spec.env.values(), *spec.headers.values()]
    return {m for text in texts for m in _VAR.findall(text)}


def expand(spec: ServerSpec, environ: Mapping[str, str]) -> tuple[ServerSpec, list[str]]:
    """``spec`` with ``${VAR}`` replaced from ``environ``, and the variables that
    are missing or empty (a server with any is skipped, not an error)."""
    missing = sorted(v for v in referenced_vars(spec) if not environ.get(v))

    def sub(text: str | None) -> str | None:
        return None if text is None else _VAR.sub(lambda m: environ.get(m.group(1), ""), text)

    expanded = spec.model_copy(
        update={
            "command": sub(spec.command),
            "url": sub(spec.url),
            "cwd": sub(spec.cwd),
            "args": [sub(a) for a in spec.args],
            "env": {k: sub(v) for k, v in spec.env.items()},
            "headers": {k: sub(v) for k, v in spec.headers.items()},
        }
    )
    return expanded, missing
