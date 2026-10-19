"""/mcp: the MCP servers (data/mcp.json), their tools, and turning them on/off.

Adding a server is never done here: only by editing data/mcp.json by hand,
then /mcp recarregar.
"""

from __future__ import annotations

from typing import Any

from uva.clock import start_of_day, utcnow
from uva.commands.registry import CommandContext, CommandRegistry
from uva.i18n import t
from uva.logging.events import EventQuery, EventType
from uva.mcp.client import McpCallError, McpUnavailable
from uva.replies import Reply
from uva.services import Services

STATE_ICONS = {"ready": "🟢", "starting": "🟡", "error": "🔴", "disabled": "⏸", "skipped": "⚪", "stopped": "⏸"}
SUBCOMMANDS = {
    "ferramentas": "tools", "tools": "tools", "ferramenta": "tools",
    "ligar": "on", "on": "on", "enable": "on", "ativar": "on",
    "desligar": "off", "off": "off", "disable": "off", "desativar": "off",
    "recarregar": "reload", "reload": "reload",
    "aprovar": "approve", "approve": "approve",
    "status": "status", "servidores": "status", "servers": "status",
}


async def calls_today(services: Services) -> tuple[int, int]:
    """(calls, failures) of MCP tools since local midnight (held writes don't count)."""
    if services.events is None:
        return 0, 0
    query = EventQuery(since=start_of_day(services.tz, utcnow()), types={EventType.MCP_CALL}, limit=5000)
    found = [e for e in await services.events.find(query) if not e.data.get("held")]
    return len(found), sum(1 for e in found if not e.data.get("ok"))


async def status_line(services: Services) -> str | None:
    """One /status line: servers ready, tools, calls today."""
    mcp = services.mcp
    if mcp is None:
        return None
    try:
        servers = (await mcp.gateway.servers()).get("servers", [])
    except (McpUnavailable, McpCallError):
        return t("commands.mcp.status_down")
    ready = sum(1 for s in servers if s.get("state") == "ready")
    tools = sum(1 for i in mcp.tools if i.status == "ok" and i.server not in _off(mcp))
    calls, failed = await calls_today(services)
    return t("commands.mcp.status_line", ready=ready, servers=len(servers), tools=tools, calls=calls, failed=failed)


def _off(mcp) -> set[str]:
    return {i.server for i in mcp.tools if i.status == "server_off"}


def state_text(server: dict[str, Any]) -> str:
    state = server.get("state", "?")
    text = t(f"commands.mcp.state.{state}") if state in STATE_ICONS else state
    if server.get("error") and state in ("error", "skipped", "starting"):
        text += f" ({server['error'][:150]})"
    return text


async def list_servers(services: Services) -> Reply:
    mcp = services.mcp
    try:
        data = await mcp.gateway.servers()
    except (McpUnavailable, McpCallError) as exc:
        return Reply(t("commands.mcp.gateway_down", error=str(exc)))
    await mcp.sync(services.actions)
    servers = data.get("servers", [])
    if not servers:
        return Reply(t("commands.mcp.no_servers"))
    off = _off(mcp)
    lines = [t("commands.mcp.header")]
    for server in servers:
        name = server["name"]
        icon = "⏸" if name in off else STATE_ICONS.get(server.get("state"), "❔")
        state = t("commands.mcp.state.off") if name in off else state_text(server)
        usable = sum(1 for i in mcp.tools if i.server == name and i.status == "ok")
        lines.append(t("commands.mcp.server_line", icon=icon, name=name, transport=server.get("transport"),
                       state=state, tools=usable))
        if server.get("description"):
            lines.append(f"   {server['description']}")
    if data.get("config_error") or mcp.config_error:
        lines.append(t("commands.mcp.config_error", error=data.get("config_error") or mcp.config_error))
    calls, failed = await calls_today(services)
    lines.append(t("commands.mcp.today", calls=calls, failed=failed))
    lines.append(t("commands.mcp.hint"))
    return Reply("\n".join(lines))


def tool_flags(info) -> str:
    if info.status == "flagged":
        return t("commands.mcp.flag.flagged", reason=info.reason)
    if info.status == "changed":
        return t("commands.mcp.flag.changed", server=info.server)
    if info.status == "bad_schema":
        return t("commands.mcp.flag.bad_schema", reason=info.reason)
    if info.status == "server_off":
        return t("commands.mcp.flag.server_off")
    flags = [t("commands.mcp.flag.read") if info.read else t("commands.mcp.flag.write")]
    if not info.read:
        flags.append(t("commands.mcp.flag.confirm") if info.confirm else t("commands.mcp.flag.trusted"))
    if info.destructive:
        flags.append(t("commands.mcp.flag.destructive"))
    if info.proactive:
        flags.append(t("commands.mcp.flag.proactive"))
    if info.long_running:
        flags.append(t("commands.mcp.flag.slow"))
    return ", ".join(flags)


async def list_tools(services: Services, server: str) -> Reply:
    mcp = services.mcp
    await mcp.sync(services.actions)
    infos = [i for i in mcp.tools if i.server == server]
    if not infos:
        return Reply(t("commands.mcp.no_tools", server=server))
    lines = [t("commands.mcp.tools_header", server=server, n=len(infos))]
    for info in infos:
        lines.append(f"• {info.tool} ({tool_flags(info)})")
        if info.description:
            lines.append(f"   {info.description[:120]}")
    return Reply("\n".join(lines))


async def toggle(services: Services, server: str, enabled: bool) -> Reply:
    mcp = services.mcp
    if mcp.store is None:
        return Reply(t("commands.mcp.no_store"))
    await mcp.store.set_override(server, enabled)
    note = ""
    try:
        await mcp.gateway.set_enabled(server, enabled)
    except (McpUnavailable, McpCallError) as exc:
        note = "\n" + t("commands.mcp.gateway_later", error=str(exc))
    await mcp.bump()
    await mcp.sync(services.actions, force=True)
    key = "commands.mcp.turned_on" if enabled else "commands.mcp.turned_off"
    return Reply(t(key, server=server) + note)


async def reload(services: Services) -> Reply:
    mcp = services.mcp
    try:
        result = await mcp.gateway.reload()
    except (McpUnavailable, McpCallError) as exc:
        return Reply(t("commands.mcp.gateway_down", error=str(exc)))
    if not result.get("ok"):
        return Reply(t("commands.mcp.config_error", error=result.get("error")))
    await mcp.bump()
    await mcp.sync(services.actions, force=True)
    return Reply(t("commands.mcp.reloaded", servers=", ".join(result.get("servers", [])) or "-"))


async def approve(services: Services, server: str) -> Reply:
    changed = await services.mcp.approve(server, services.actions)
    if not changed:
        return Reply(t("commands.mcp.nothing_to_approve", server=server))
    return Reply(t("commands.mcp.approved", server=server, tools=", ".join(changed)))


def register(registry: CommandRegistry) -> None:
    @registry.command(
        "mcp",
        t("commands.mcp.mcp.description"),
        aliases=("servidores_mcp", "mcp_servers"),
        usage=t("commands.mcp.mcp.usage_arg"),
        examples=("servidores mcp", "ferramentas mcp", "quais servidores mcp tenho"),
    )
    async def mcp_command(ctx: CommandContext) -> Reply:
        services = ctx.services
        if services.mcp is None:
            return Reply(t("commands.mcp.not_configured"))
        words = [w.lower() for w in ctx.args]
        sub = SUBCOMMANDS.get(words[0]) if words else "status"
        if sub is None:
            return Reply(t("commands.mcp.usage"))
        if sub == "status":
            return await list_servers(services)
        if sub == "reload":
            return await reload(services)
        if len(words) < 2:
            return Reply(t("commands.mcp.usage"))
        server = words[1]
        known = services.mcp.specs or {}
        if not known:
            await services.mcp.sync(services.actions, force=True)
            known = services.mcp.specs
        if server not in known:
            # Servers come only from data/mcp.json, edited by hand.
            return Reply(t("commands.mcp.unknown_server", server=server, servers=", ".join(sorted(known)) or "-"))
        if sub == "tools":
            return await list_tools(services, server)
        if sub == "approve":
            return await approve(services, server)
        return await toggle(services, server, sub == "on")
