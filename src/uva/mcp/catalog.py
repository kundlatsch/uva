"""MCP tools as UVA actions: the gateway's catalog + the policy in data/mcp.json.

Every allowed tool becomes an action ``mcp_<server>_<tool>`` in the ``mcp``
group of the action registry, validated with its own JSON Schema. Reads are
lookups (their result goes back to the LLM in the same turn); everything else
is a write. The bot and the worker keep the group in sync: whenever the
catalog version in Redis changes (checked on each message, a cheap GET), the
group is rebuilt. The bot never adds servers: only data/mcp.json does.
"""

from __future__ import annotations

import json
import math
from collections.abc import Awaitable, Callable
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from uva.actions.registry import Action, ActionContext, ActionRegistry, ActionResult, JsonArgs
from uva.config import Settings
from uva.i18n import t
from uva.logging import get_logger
from uva.logging.events import EventType, emit
from uva.mcp.client import McpCallError, McpGateway, McpUnavailable
from uva.mcp.config import McpConfigError, Policy, ServerSpec, load_config, matches
from uva.llm.budget import Scope
from uva.safety import as_data, describe, sanitize_untrusted, scan_untrusted

if TYPE_CHECKING:
    from uva.services import Services

log = get_logger(__name__)

GROUP = "mcp"
VERSION_KEY = "mcp:catalog_version"  # the gateway's catalog version, as last seen
BUMP_KEY = "mcp:catalog_bump"  # bumped by /mcp aprovar, ligar, desligar, recarregar
CHECK_SECONDS = 30  # how often a process asks the gateway for its version
NAME_LIMIT = 64
DESCRIPTION_CHARS = 150
SECRET_KEYS = re.compile(r"token|secret|passw|api[_-]?key|authorization|cookie|credential", re.IGNORECASE)


@dataclass(slots=True)
class ToolInfo:
    """One tool as UVA sees it (also the blocked ones, for /mcp ferramentas)."""

    server: str
    tool: str
    action: str
    description: str
    read: bool
    proactive: bool
    long_running: bool
    confirm: bool
    destructive: bool
    hash: str
    status: str = "ok"  # ok | bad_schema | flagged | changed | server_off
    reason: str = ""


def action_name(server: str, tool: str, taken: set[str]) -> str:
    """mcp_<server>_<tool>: lowercase, non-alphanumerics -> "_", max 64, unique."""
    base = re.sub(r"[^a-z0-9]+", "_", f"mcp_{server}_{tool}".lower()).strip("_")[:NAME_LIMIT]
    name, n = base, 2
    while name in taken:
        suffix = f"_{n}"
        name, n = base[: NAME_LIMIT - len(suffix)] + suffix, n + 1
    return name


def mask(value: Any) -> Any:
    """Arguments for the log, with secret-looking keys hidden."""
    if isinstance(value, dict):
        return {k: "***" if SECRET_KEYS.search(str(k)) else mask(v) for k, v in value.items()}
    if isinstance(value, list):
        return [mask(v) for v in value]
    return value


def render_result(result: dict[str, Any]) -> str:
    """Text blocks joined; resource links listed; images only mentioned (v1)."""
    parts: list[str] = []
    for block in result.get("content") or []:
        kind = block.get("type")
        if kind == "text":
            parts.append(block.get("text") or "")
        elif kind == "resource":
            parts.append(t("mcp.block.resource", uri=block.get("uri"), text=block.get("text") or "").rstrip())
        elif kind == "resource_link":
            parts.append(t("mcp.block.link", name=block.get("name") or "", uri=block.get("uri")))
        else:
            parts.append(t("mcp.block.other", kind=kind, mime=block.get("mime_type") or ""))
    if not any(parts) and result.get("structured") is not None:
        parts.append(json.dumps(result["structured"], ensure_ascii=False))
    return "\n\n".join(p for p in parts if p)


def clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n" + t("mcp.clipped", total=len(text))


def check_schema(schema: Any) -> str | None:
    """Why this input schema can't be used (None = fine)."""
    import jsonschema

    if not isinstance(schema, dict) or schema.get("type", "object") != "object":
        return "the input schema is not an object"
    try:
        jsonschema.validators.validator_for(schema, default=jsonschema.Draft202012Validator).check_schema(schema)
    except jsonschema.SchemaError as exc:
        return f"invalid input schema: {exc.message[:100]}"
    return None


def classify(tool: dict[str, Any], policy: Policy) -> dict[str, bool]:
    name = tool["name"]
    annotations = tool.get("annotations") or {}
    read = policy.is_read(name, annotations)
    return {
        "read": read,
        "proactive": read and matches(name, policy.proactive_tools),
        # Writes never run in the background: they need a "yes" in the chat first.
        "long_running": read and matches(name, policy.long_running_tools),
        "confirm": not read and policy.needs_confirmation(name),
        "destructive": bool(annotations.get("destructiveHint")) and not read,
    }


def build_actions(
    tools: list[dict[str, Any]],
    specs: dict[str, ServerSpec],
    *,
    prompt_limit: int,
    approved: dict[str, str] | None = None,
    disabled: frozenset[str] | set[str] = frozenset(),
) -> tuple[list[Action], list[ToolInfo]]:
    """Actions for every usable tool, and what UVA thinks of each tool.

    Not registered: tools whose description/schema looks like instructions to
    the assistant (tool poisoning), tools whose definition changed since it was
    approved (``approved``: "server/tool" -> hash; unseen tools are new, not
    changed) and tools of servers turned off with /mcp desligar."""
    actions: list[Action] = []
    infos: list[ToolInfo] = []
    taken: set[str] = {"mcp_tools"}
    for tool in tools:
        spec = specs.get(tool.get("server", ""))
        if spec is None or not spec.policy.allows(tool.get("name", "")):
            continue  # not in mcp.json (any more), or denied by the policy
        flags = classify(tool, spec.policy)
        name = action_name(spec.name, tool["name"], taken)
        description = " ".join((tool.get("description") or "").split())
        info = ToolInfo(spec.name, tool["name"], name, description, hash=tool.get("hash", ""), **flags)
        infos.append(info)
        problem = check_schema(tool.get("input_schema"))
        definition = f"{tool['name']}\n{tool.get('description') or ''}\n{json.dumps(tool.get('input_schema'))}"
        findings = scan_untrusted(definition)
        known = (approved or {}).get(tool_key(spec.name, tool["name"]))
        if spec.name in disabled:
            info.status = "server_off"
        elif problem:
            info.status, info.reason = "bad_schema", problem
        elif findings:
            info.status, info.reason = "flagged", describe(findings)[:200]
        elif known is not None and known != info.hash:
            info.status = "changed"
        if info.status != "ok":
            continue
        taken.add(name)
        actions.append(_tool_action(info, spec, tool["input_schema"]))
    if len(actions) > prompt_limit:
        actions.append(_list_action(infos))
    return actions, infos


def tool_key(server: str, tool: str) -> str:
    return f"{server}/{tool}"


def _tool_action(info: ToolInfo, spec: ServerSpec, schema: dict[str, Any]) -> Action:
    short = info.description[:DESCRIPTION_CHARS] or info.tool
    marker = "" if info.read else (" [destructive write, asks the user]" if info.destructive else " [write, asks the user]")
    return Action(
        info.action,
        f"({info.server}) {short}{marker}",
        JsonArgs,
        _handler(info, spec.policy),
        proactive_safe=info.proactive,
        long_running=info.long_running,
        progress_phrase="pesquisando" if info.long_running else None,
        lookup=info.read and not info.long_running,
        json_schema=schema,
        group=GROUP,
        meta={
            "server": info.server,
            "tool": info.tool,
            "server_description": spec.policy.description,
            "read": info.read,
            "confirm": info.confirm,
            "destructive": info.destructive,
        },
    )


def _handler(info: ToolInfo, policy: Policy):
    async def handler(params: JsonArgs, ctx: ActionContext) -> ActionResult:
        return await call_tool(info, policy, params.model_dump(), ctx)

    return handler


async def call_tool(info: ToolInfo, policy: Policy, arguments: dict[str, Any], ctx: ActionContext) -> ActionResult:
    """Call the tool through the gateway; the result is scanned, clipped and,
    for the LLM, marked as data."""
    mcp = ctx.services.mcp
    started = time.perf_counter()
    logged = {"server": info.server, "tool": info.tool, "arguments": mask(arguments), "scope": ctx.scope.value}
    if mcp is None:
        return ActionResult(text=t("mcp.disabled"), data={"ok": False, "error": "mcp disabled"})
    if not info.read and ctx.scope is not Scope.INTERACTIVE:
        # Goals, initiatives, the review: writes are refused, never queued for a "yes".
        await emit(EventType.MCP_CALL, **logged, ok=False, refused=True)
        return ActionResult(text=t("mcp.write_refused", tool=tool_key(info.server, info.tool)), data={"ok": False})
    if info.confirm and not ctx.confirmed:
        return await _hold(info, arguments, ctx, logged)
    try:
        result = await mcp.gateway.call(info.server, info.tool, arguments, policy.timeout_s)
    except (McpUnavailable, McpCallError) as exc:
        duration = round((time.perf_counter() - started) * 1000)
        await emit(EventType.MCP_CALL, **logged, ok=False, error=str(exc), duration_ms=duration)
        key = "mcp.unavailable" if isinstance(exc, McpUnavailable) else "mcp.call_failed"
        return ActionResult(
            text=t(key, server=info.server, tool=info.tool, error=str(exc)),
            data={"ok": False, "error": str(exc)},
        )
    raw = render_result(result)
    clean, findings = sanitize_untrusted(raw)
    text = clip(clean, policy.max_result_chars) or t("mcp.empty")
    failed = bool(result.get("is_error"))
    await emit(
        EventType.MCP_CALL,
        **logged,
        ok=not failed,
        duration_ms=result.get("duration_ms"),
        chars=len(raw),
        clipped=len(clean) > policy.max_result_chars,
        injection=[f.kind for f in findings] or None,
    )
    label = f"{info.server}/{info.tool}"
    if failed:
        text = t("mcp.tool_error", tool=label) + "\n" + text
    if info.read and not info.long_running:
        out = as_data(f"mcp result {label}", text)  # goes back to the LLM
    else:
        out = t("mcp.result", tool=label) + "\n" + text  # goes to the user
    data = {"ok": not failed, "server": info.server, "tool": info.tool, "chars": len(raw)}
    if failed:
        data["error"] = text[:300]
    return ActionResult(text=out, data=data)


CONFIRM_ARGS_CHARS = 1200


async def _hold(info: ToolInfo, arguments: dict[str, Any], ctx: ActionContext, logged: dict) -> ActionResult:
    """A write: show the exact call and ask (Sim/Nao) before anything runs."""
    from uva.replies import Button

    shown = json.dumps(mask(arguments), ensure_ascii=False, indent=2)
    if len(shown) > CONFIRM_ARGS_CHARS:
        shown = shown[:CONFIRM_ARGS_CHARS] + "\n..."
    token = ctx.services.held_writes.add(info.action, arguments, kind="mcp")
    await emit(EventType.MCP_CALL, **logged, ok=False, held=token)
    key = "mcp.confirm_destructive" if info.destructive else "mcp.confirm"
    return ActionResult(
        text=t(key, server=info.server, tool=info.tool, arguments=shown),
        data={"ok": False, "held": token},
        buttons=[Button(t("mcp.yes"), f"wrok:{token}"), Button(t("router.write.no"), f"wrno:{token}")],
    )


class McpToolsParams(BaseModel):
    server: str = Field(description="MCP server name")


def _list_action(infos: list[ToolInfo]) -> Action:
    usable = [i for i in infos if i.status == "ok"]

    async def handler(params: McpToolsParams, ctx: ActionContext) -> ActionResult:
        from uva.llm.prompts import action_schema

        lines = []
        actions = ctx.services.actions
        for info in usable:
            if info.server != params.server:
                continue
            action = actions.get(info.action) if actions is not None else None
            if action is None:
                continue
            schema = action_schema(action)
            required = set(schema.get("required", []))
            names = ", ".join(n if n in required else f"{n}?" for n in schema.get("properties", {}))
            lines.append(f"- {action.name}({names}): {action.description}")
        if not lines:
            servers = sorted({i.server for i in usable})
            return ActionResult(text=t("mcp.no_server", server=params.server, servers=", ".join(servers)))
        return ActionResult(text="\n".join(lines), data={"server": params.server, "tools": len(lines)})

    return Action(
        "mcp_tools",
        "Lists the tools of one MCP server (name and params); call it before using a server's tools",
        McpToolsParams,
        handler,
        proactive_safe=True,
        lookup=True,
        group=GROUP,
    )


class McpService:
    """Keeps a process's ``mcp`` action group in sync with the gateway."""

    def __init__(self, settings: Settings, gateway: McpGateway, redis: Any = None, *, store: Any = None) -> None:
        self.settings = settings
        self.gateway = gateway
        self.redis = redis
        # Approved definitions and /mcp ligar|desligar (Mongo); None = no checks (tests).
        self.store = store
        # Sends a notice to the owner (bot: Telegram; worker: runtime.notify).
        self.notifier: Callable[[str], Awaitable[None]] | None = None
        self.specs: dict[str, ServerSpec] = {}
        self.tools: list[ToolInfo] = []
        self.config_error: str | None = None
        self._checked = -math.inf
        self._gateway_version: str | None = None
        self._local_bump = 0
        self._synced: tuple[str | None, str] | None = None

    @property
    def config_path(self):
        return self.settings.repo_dir / "data" / "mcp.json"

    async def bump(self) -> None:
        """Every process rebuilds its group on its next message/task."""
        self._local_bump += 1
        if self.redis is not None:
            try:
                await self.redis.incr(BUMP_KEY)
            except Exception as exc:
                log.warning("mcp_bump_failed", error=str(exc))

    async def _version(self, force: bool) -> tuple[str | None, str]:
        now = time.monotonic()
        if force or now - self._checked >= CHECK_SECONDS:
            self._checked = now
            try:
                seen = await self.gateway.version()
            except (McpUnavailable, McpCallError):
                seen = "down"
            self._gateway_version = seen
            if self.redis is not None:
                try:
                    await self.redis.set(VERSION_KEY, seen)
                except Exception:
                    pass
        if self.redis is not None:
            try:
                version, bump = await self.redis.mget(VERSION_KEY, BUMP_KEY)
                return version, f"{bump or 0}"
            except Exception:
                pass
        return self._gateway_version, f"local{self._local_bump}"

    async def sync(self, registry: ActionRegistry | None, *, force: bool = False) -> bool:
        """Rebuild the group when the catalog changed. Never raises."""
        if registry is None:
            return False
        try:
            version = await self._version(force)
            if not force and version == self._synced:
                return False
            if not await self.rebuild(registry):
                return False
            self._synced = version
            return True
        except Exception as exc:  # MCP must never block an answer
            log.warning("mcp_sync_failed", error=repr(exc))
            return False

    async def rebuild(self, registry: ActionRegistry) -> bool:
        try:
            self.specs = load_config(self.config_path)
            self.config_error = None
        except McpConfigError as exc:
            self.specs, self.config_error = {}, str(exc)
        try:
            tools = (await self.gateway.catalog()).get("tools", [])
        except (McpUnavailable, McpCallError) as exc:
            log.warning("mcp_catalog_unavailable", error=str(exc))
            return False  # keep what we had: calls answer "gateway down" on their own
        approved, overrides = await self._state()
        disabled = {name for name, enabled in overrides.items() if not enabled}
        actions, infos = build_actions(
            tools, self.specs, prompt_limit=self.settings.mcp_prompt_tool_limit,
            approved=approved, disabled=disabled,
        )
        registry.replace_group(GROUP, actions)
        self.tools = infos
        await self._after_build(infos, approved, overrides)
        log.info("mcp_catalog_synced", tools=len(actions), blocked=sum(i.status != "ok" for i in infos))
        return True


    async def _state(self) -> tuple[dict[str, str] | None, dict[str, bool]]:
        if self.store is None:
            return None, {}
        return await self.store.approved(), await self.store.overrides()

    async def _after_build(
        self, infos: list[ToolInfo], approved: dict[str, str] | None, overrides: dict[str, bool]
    ) -> None:
        if self.store is None or approved is None:
            return
        # First sight: a tool's definition is approved as it is now.
        new = {
            tool_key(i.server, i.tool): i.hash
            for i in infos
            if i.status == "ok" and tool_key(i.server, i.tool) not in approved
        }
        if new:
            await self.store.approve(new)
        changed: dict[str, list[str]] = {}
        for info in infos:
            key = tool_key(info.server, info.tool)
            if info.status == "changed" and await self.store.mark_notified(key, info.hash):
                changed.setdefault(info.server, []).append(info.tool)
            elif info.status == "flagged" and await self.store.mark_notified(key, "flagged:" + info.hash):
                await emit(EventType.ERROR, where="mcp_tool_flagged", server=info.server, tool=info.tool,
                           findings=info.reason)
        for server, names in changed.items():
            await emit(EventType.ERROR, where="mcp_tool_changed", server=server, tools=names)
            await self.notify(t("mcp.changed_notice", server=server, tools=", ".join(sorted(names))))
        await self._push_overrides(overrides)

    async def _push_overrides(self, overrides: dict[str, bool]) -> None:
        """/mcp ligar|desligar live in Mongo: re-apply them after a gateway restart."""
        if not overrides:
            return
        try:
            states = {s["name"]: s["state"] for s in (await self.gateway.servers()).get("servers", [])}
        except (McpUnavailable, McpCallError):
            return
        for name, enabled in overrides.items():
            state = states.get(name)
            if state is None:
                continue
            if (enabled and state == "disabled") or (not enabled and state not in ("disabled", "skipped", "stopped")):
                try:
                    await self.gateway.set_enabled(name, enabled)
                except (McpUnavailable, McpCallError) as exc:
                    log.warning("mcp_override_failed", server=name, error=str(exc))

    async def notify(self, text: str) -> None:
        if self.notifier is None:
            log.info("mcp_notice", text=text)
            return
        try:
            await self.notifier(text)
        except Exception as exc:
            log.warning("mcp_notice_failed", error=str(exc))

    async def approve(self, server: str, registry: ActionRegistry | None) -> list[str]:
        """/mcp aprovar: accept the current definitions of a server's tools."""
        if registry is not None:
            await self.sync(registry, force=True)
        mine = [i for i in self.tools if i.server == server and i.status in ("ok", "changed")]
        changed = sorted(i.tool for i in mine if i.status == "changed")
        if self.store is not None and mine:
            await self.store.approve({tool_key(i.server, i.tool): i.hash for i in mine})
        await self.bump()
        if registry is not None:
            await self.sync(registry, force=True)
        return changed


async def sync_actions(services: Services, *, force: bool = False) -> bool:
    """Convenience for the agent, the worker and the goal loop."""
    if services.mcp is None:
        return False
    return await services.mcp.sync(services.actions, force=force)
