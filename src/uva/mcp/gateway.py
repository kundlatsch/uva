"""The ``mcp`` gateway container: runs the MCP servers and keeps one session each.

MCP servers are third-party code, so they never run inside the bot's
containers. This process (``python -m uva.mcp.gateway``) reads data/mcp.json,
starts every enabled server (stdio subprocess or Streamable HTTP) with the
official ``mcp`` SDK, restarts crashed ones with backoff, caches each server's
tool list and serves a small internal HTTP API on the ``mcpnet`` network only,
protected by ``MCP_GATEWAY_TOKEN``:

  GET  /health                     liveness (no token, no details)
  GET  /servers                    name, transport, state, error, tool count
  GET  /catalog                    every allowed tool with its schema and hash
  GET  /version                    hash of the catalog (changes when it changes)
  POST /call                       {server, tool, arguments, timeout_s}
  POST /reload                     re-read data/mcp.json
  POST /servers/{name}/restart
  POST /servers/{name}/enabled     {"enabled": bool}, runtime only

The gateway sees only its own environment (mcp.env): no bot secrets, no repo,
no Mongo, no Redis. Stdio servers inherit only the SDK's safe variables plus
the ``env`` written for them in mcp.json. Sampling and elicitation are never
offered to servers (no callbacks): a server cannot spend UVA's LLM budget nor
ask the user anything.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from uva.mcp.config import McpConfigError, ServerSpec, expand, load_config

log = logging.getLogger("uva.mcp.gateway")

DEFAULT_CONFIG = "/config/mcp.json"
DEFAULT_PORT = 8790
# Tool lists are fetched again this often (also a liveness check, and how a
# changed tool definition is noticed when the server sends no list_changed).
REFRESH_SECONDS = 300
CONNECT_TIMEOUT_S = 60
BACKOFF_MIN_S = 2
BACKOFF_MAX_S = 300
MAX_TEXT_BLOCK = 100_000
LIST_PAGES = 20


class GatewayError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def tool_hash(name: str, description: str, schema: dict[str, Any]) -> str:
    """What a definition is: approved once, a change disables the tool (rug pull)."""
    payload = json.dumps({"n": name, "d": description, "s": schema}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


@dataclass
class ServerRuntime:
    spec: ServerSpec
    state: str = "stopped"  # starting | ready | error | disabled | skipped | stopped
    error: str | None = None
    tools: list[dict[str, Any]] = field(default_factory=list)
    client: Any = None
    task: asyncio.Task | None = None
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    refresh: asyncio.Event = field(default_factory=asyncio.Event)
    semaphore: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(4))
    restarts: int = 0
    started_at: float | None = None

    def summary(self) -> dict[str, Any]:
        spec = self.spec
        return {
            "name": spec.name,
            "transport": spec.type,
            "state": self.state,
            "error": self.error,
            "tools": len(self.tools),
            "restarts": self.restarts,
            "description": spec.policy.description,
        }


class Gateway:
    def __init__(
        self,
        config_path: Path,
        environ: dict[str, str] | None = None,
        *,
        refresh_seconds: float = REFRESH_SECONDS,
        backoff_min: float = BACKOFF_MIN_S,
    ) -> None:
        self.config_path = config_path
        self.environ = dict(os.environ if environ is None else environ)
        self.refresh_seconds = refresh_seconds
        self.backoff_min = backoff_min
        self.servers: dict[str, ServerRuntime] = {}
        # /mcp ligar|desligar: runtime only, mcp.json is never touched.
        self.overrides: dict[str, bool] = {}
        self.config_error: str | None = None

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        await self.reload()

    async def close(self) -> None:
        await asyncio.gather(*(self._stop(s) for s in self.servers.values()))
        self.servers = {}

    async def reload(self) -> dict[str, Any]:
        """Re-read mcp.json: stop removed/changed servers, start new ones."""
        try:
            specs = load_config(self.config_path)
            self.config_error = None
        except McpConfigError as exc:
            self.config_error = str(exc)
            log.error("mcp_config_invalid: %s", exc)
            return {"ok": False, "error": self.config_error}
        for name in list(self.servers):
            if name not in specs or specs[name] != self.servers[name].spec:
                await self._stop(self.servers.pop(name))
        for name, spec in specs.items():
            if name not in self.servers:
                self.servers[name] = ServerRuntime(spec, semaphore=asyncio.Semaphore(spec.policy.max_concurrency))
                self._launch(self.servers[name])
        return {"ok": True, "servers": sorted(self.servers)}

    async def restart(self, name: str) -> None:
        runtime = self._server(name)
        await self._stop(runtime)
        fresh = ServerRuntime(runtime.spec, semaphore=asyncio.Semaphore(runtime.spec.policy.max_concurrency))
        self.servers[name] = fresh
        self._launch(fresh)

    async def set_enabled(self, name: str, enabled: bool) -> None:
        self._server(name)
        self.overrides[name] = enabled
        await self.restart(name)

    def _enabled(self, spec: ServerSpec) -> bool:
        return self.overrides.get(spec.name, spec.policy.enabled)

    def _launch(self, runtime: ServerRuntime) -> None:
        spec = runtime.spec
        if not self._enabled(spec):
            runtime.state = "disabled"
            return
        _, missing = expand(spec, self.environ)
        if missing:
            # Not an error: the secret just isn't there yet (e.g. GITHUB_PAT).
            runtime.state = "skipped"
            runtime.error = "missing " + ", ".join(missing)
            return
        runtime.state = "starting"
        runtime.task = asyncio.create_task(self._supervise(runtime), name=f"mcp-{spec.name}")

    async def _stop(self, runtime: ServerRuntime) -> None:
        runtime.stop.set()
        runtime.refresh.set()
        task = runtime.task
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=10)
            except (TimeoutError, asyncio.CancelledError, Exception):
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
        runtime.state = "stopped"
        runtime.client = None

    # -- one server ----------------------------------------------------------

    async def _supervise(self, runtime: ServerRuntime) -> None:
        delay = self.backoff_min
        name = runtime.spec.name
        while not runtime.stop.is_set():
            runtime.state = "starting"
            try:
                await self._session(runtime)
                delay = self.backoff_min
            except asyncio.CancelledError:
                raise
            except BaseException as exc:  # anyio groups raise ExceptionGroup
                runtime.error = _short_error(exc)
                log.warning("mcp_server_failed server=%s error=%s", name, runtime.error)
            runtime.client = None
            runtime.tools = []
            if runtime.stop.is_set():
                break
            runtime.state = "error"
            runtime.restarts += 1
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(runtime.stop.wait(), timeout=delay)
            delay = min(delay * 2, BACKOFF_MAX_S)

    async def _session(self, runtime: ServerRuntime) -> None:
        """Connect, list the tools, then keep the session until stopped or broken."""
        spec, _ = expand(runtime.spec, self.environ)
        async with contextlib.AsyncExitStack() as stack:
            client = await stack.enter_async_context(_open_client(spec, runtime))
            runtime.client = client
            await self._list_tools(runtime)
            runtime.state = "ready"
            runtime.error = None
            runtime.started_at = time.time()
            log.info("mcp_server_ready server=%s tools=%d", spec.name, len(runtime.tools))
            while not runtime.stop.is_set():
                runtime.refresh.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(runtime.refresh.wait(), timeout=self.refresh_seconds)
                if runtime.stop.is_set():
                    break
                # Liveness + changed definitions; a failure ends the session (restart).
                await asyncio.wait_for(self._list_tools(runtime), timeout=CONNECT_TIMEOUT_S)

    async def _list_tools(self, runtime: ServerRuntime) -> None:
        client = runtime.client
        policy = runtime.spec.policy
        tools: list[dict[str, Any]] = []
        cursor = None
        for _ in range(LIST_PAGES):
            page = await client.list_tools(cursor=cursor)
            for tool in page.tools:
                if not policy.allows(tool.name):
                    continue
                description = tool.description or ""
                schema = tool.input_schema or {"type": "object"}
                annotations = tool.annotations.model_dump(by_alias=True, exclude_none=True) if tool.annotations else {}
                tools.append({
                    "name": tool.name,
                    "description": description,
                    "input_schema": schema,
                    "annotations": annotations,
                    "hash": tool_hash(tool.name, description, schema),
                })
            cursor = page.next_cursor
            if not cursor:
                break
        runtime.tools = tools

    # -- API -----------------------------------------------------------------

    def _server(self, name: str) -> ServerRuntime:
        runtime = self.servers.get(name)
        if runtime is None:
            raise GatewayError(404, f"unknown server: {name}")
        return runtime

    def list_servers(self) -> list[dict[str, Any]]:
        return [s.summary() for s in self.servers.values()]

    def catalog(self) -> list[dict[str, Any]]:
        return [
            {"server": s.spec.name, **tool}
            for s in self.servers.values()
            if s.state == "ready"
            for tool in s.tools
        ]

    def version(self) -> str:
        parts = [
            [s.spec.name, s.state, [t["hash"] for t in s.tools] if s.state == "ready" else []]
            for s in sorted(self.servers.values(), key=lambda r: r.spec.name)
        ]
        return hashlib.sha256(json.dumps(parts).encode()).hexdigest()[:16]

    async def call(
        self, server: str, tool: str, arguments: dict[str, Any] | None, timeout_s: float | None = None
    ) -> dict[str, Any]:
        runtime = self._server(server)
        if runtime.state != "ready" or runtime.client is None:
            raise GatewayError(503, f"server {server} is {runtime.state}" + (f": {runtime.error}" if runtime.error else ""))
        if not any(t["name"] == tool for t in runtime.tools):
            raise GatewayError(404, f"unknown or blocked tool: {server}/{tool}")
        timeout = min(timeout_s or runtime.spec.policy.timeout_s, 600)
        started = time.perf_counter()
        async with runtime.semaphore:
            try:
                async with asyncio.timeout(timeout):
                    result = await runtime.client.call_tool(tool, arguments or {}, read_timeout_seconds=timeout)
            except TimeoutError as exc:
                raise GatewayError(504, f"{server}/{tool} timed out after {timeout:g}s") from exc
            except Exception as exc:
                runtime.refresh.set()  # check the session right away
                raise GatewayError(502, f"{server}/{tool} failed: {_short_error(exc)}") from exc
        return {
            "content": [_block(b) for b in result.content],
            "is_error": bool(result.is_error),
            "structured": result.structured_content,
            "duration_ms": round((time.perf_counter() - started) * 1000),
        }


@contextlib.asynccontextmanager
async def _open_client(spec: ServerSpec, runtime: ServerRuntime):
    from mcp import Client
    from mcp.client.stdio import StdioServerParameters
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client
    from mcp_types import Implementation

    async def on_message(message: Any) -> None:
        # notifications/tools/list_changed: fetch the list again now.
        method = getattr(message, "method", None) or getattr(getattr(message, "root", None), "method", None)
        if method == "notifications/tools/list_changed":
            runtime.refresh.set()

    common = dict(
        mode="auto",
        read_timeout_seconds=CONNECT_TIMEOUT_S,
        message_handler=on_message,
        client_info=Implementation(name="uva", version="1"),
        cache=None,
        # No sampling/elicitation/roots callbacks: those capabilities are not offered.
    )
    async with contextlib.AsyncExitStack() as stack:
        if spec.type == "stdio":
            target: Any = StdioServerParameters(
                command=spec.command or "", args=spec.args, env=spec.env or None, cwd=spec.cwd
            )
        else:
            http = await stack.enter_async_context(create_mcp_http_client(headers=spec.headers or None))
            target = streamable_http_client(spec.url or "", http_client=http)
        async with asyncio.timeout(CONNECT_TIMEOUT_S):
            client = await stack.enter_async_context(Client(target, **common))
        yield client


def _block(block: Any) -> dict[str, Any]:
    kind = getattr(block, "type", "unknown")
    if kind == "text":
        return {"type": "text", "text": block.text[:MAX_TEXT_BLOCK]}
    if kind in ("image", "audio"):
        return {"type": kind, "mime_type": block.mime_type, "size": len(block.data or "")}
    if kind == "resource_link":
        return {"type": "resource_link", "uri": str(block.uri), "name": block.name}
    if kind == "resource":
        resource = block.resource
        text = getattr(resource, "text", None)
        return {"type": "resource", "uri": str(resource.uri), "text": text[:MAX_TEXT_BLOCK] if text else None}
    return {"type": str(kind)}


def _short_error(exc: BaseException) -> str:
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    text = str(exc) or exc.__class__.__name__
    return f"{exc.__class__.__name__}: {text}"[:300]


# -- HTTP --------------------------------------------------------------------


class CallBody(BaseModel):
    server: str
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    timeout_s: float | None = None


class EnabledBody(BaseModel):
    enabled: bool


def create_app(gateway: Gateway, token: str, *, manage: bool = True) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(_app):
        if manage:
            await gateway.start()
        try:
            yield
        finally:
            if manage:
                await gateway.close()

    def authorized(request: Request) -> None:
        sent = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        # No token configured = nothing is served (never an open gateway).
        if not token or not hmac.compare_digest(sent.encode(), token.encode()):
            raise HTTPException(401, "unauthorized")

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    auth = [Depends(authorized)]

    @app.exception_handler(GatewayError)
    async def _gateway_error(_request: Request, exc: GatewayError):
        return JSONResponse({"error": str(exc)}, status_code=exc.status)

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.get("/servers", dependencies=auth)
    async def servers():
        return {"servers": gateway.list_servers(), "config_error": gateway.config_error, "version": gateway.version()}

    @app.get("/catalog", dependencies=auth)
    async def catalog():
        return {"tools": gateway.catalog(), "version": gateway.version()}

    @app.get("/version", dependencies=auth)
    async def version():
        return {"version": gateway.version()}

    @app.post("/call", dependencies=auth)
    async def call(body: CallBody):
        return await gateway.call(body.server, body.tool, body.arguments, body.timeout_s)

    @app.post("/reload", dependencies=auth)
    async def reload():
        return await gateway.reload()

    @app.post("/servers/{name}/restart", dependencies=auth)
    async def restart(name: str):
        await gateway.restart(name)
        return {"ok": True}

    @app.post("/servers/{name}/enabled", dependencies=auth)
    async def enabled(name: str, body: EnabledBody):
        await gateway.set_enabled(name, body.enabled)
        return {"ok": True}

    return app


def main() -> None:
    import uvicorn

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    token = os.environ.get("MCP_GATEWAY_TOKEN", "")
    if not token:
        log.error("MCP_GATEWAY_TOKEN is not set: every request will be refused")
    gateway = Gateway(Path(os.environ.get("MCP_CONFIG", DEFAULT_CONFIG)))
    app = create_app(gateway, token)
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("MCP_PORT", DEFAULT_PORT)), log_level="warning")


if __name__ == "__main__":
    main()
