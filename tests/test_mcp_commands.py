"""/mcp, the /status line, /log mcp and the dashboard's MCP tab."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from mcp_fakes import InMemoryMcpStore, mcp_services, tool

from uva import i18n
from uva.actions.registry import ActionContext
from uva.commands.log import parse_log_args
from uva.commands.status import collect_status
from uva.llm.agent import execute_action
from uva.logging.events import EventType
from uva.router import Router

SERVERS = {
    "brain": {"command": "x", "uva": {"description": "The vault, read-only", "read_tools": ["*"], "proactive_tools": ["search_files"]}},
    "github": {"url": "https://example.com/mcp", "headers": {"Authorization": "Bearer ${GITHUB_PAT}"}},
}
TOOLS = [
    tool("brain", "search_files", "Searches files"),
    tool("brain", "read_text_file", "Reads a file"),
]
GATEWAY_SERVERS = [
    {"name": "brain", "transport": "stdio", "state": "ready", "error": None, "tools": 2, "description": "The vault, read-only"},
    {"name": "github", "transport": "http", "state": "skipped", "error": "missing GITHUB_PAT", "tools": 0, "description": ""},
]


@pytest.fixture
async def mcp(tmp_path, event_store):
    store = InMemoryMcpStore()
    services, gateway = mcp_services(tmp_path, SERVERS, list(TOOLS), store=store)
    services.events = event_store
    gateway.servers_list = [dict(s) for s in GATEWAY_SERVERS]
    await services.mcp.sync(services.actions)
    return services, gateway, store


async def command(services, text: str) -> str:
    return (await Router(services).handle_text(text)).text


async def test_mcp_lists_the_servers(mcp):
    services, gateway, _ = mcp
    action, params = services.actions.validate("mcp_brain_search_files", {"text": "x"})
    await execute_action(action, params, ActionContext(services))
    text = await command(services, "/mcp")
    assert "🟢 brain (stdio): pronto, 2 ferramentas" in text
    assert "The vault, read-only" in text
    assert "⚪ github (http): pulado (missing GITHUB_PAT)" in text
    assert "Hoje: 1 chamadas, 0 com falha." in text


async def test_mcp_tools_shows_the_flags(mcp):
    services, gateway, _ = mcp
    text = await command(services, "/mcp ferramentas brain")
    assert "• search_files (leitura, proativa)" in text
    assert "• read_text_file (leitura)" in text
    i18n.set_language("en")
    text = await command(services, "/mcp tools brain")
    assert "• search_files (read, proactive)" in text


async def test_turning_a_server_off_and_on(mcp):
    services, gateway, store = mcp
    text = await command(services, "/mcp desligar brain")
    assert "desligado" in text
    assert store.servers == {"brain": False} and gateway.toggled[-1] == ("brain", False)
    assert services.actions.get("mcp_brain_search_files") is None
    assert "⏸ brain" in await command(services, "/mcp")
    text = await command(services, "/mcp ligar brain")
    assert "ligado" in text and store.servers == {"brain": True}
    assert services.actions.get("mcp_brain_search_files") is not None


async def test_only_servers_in_mcp_json_exist(mcp):
    services, gateway, store = mcp
    text = await command(services, "/mcp ligar notion")
    assert "Não existe servidor MCP notion" in text and "data/mcp.json" in text
    assert store.servers == {} and gateway.toggled == []


async def test_reload_and_approve(mcp):
    services, gateway, _ = mcp
    assert "recarregado" in await command(services, "/mcp recarregar")
    assert gateway.reloads == 1
    assert "Nada para aprovar" in await command(services, "/mcp aprovar brain")
    gateway.tools = [tool("brain", "search_files", "Searches files, differently"), TOOLS[1]]
    gateway.ver = "v2"
    await services.mcp.sync(services.actions, force=True)
    assert services.actions.get("mcp_brain_search_files") is None
    assert "definição mudou" in await command(services, "/mcp ferramentas brain")
    text = await command(services, "/mcp aprovar brain")
    assert "search_files" in text and services.actions.get("mcp_brain_search_files") is not None


async def test_usage_and_gateway_down(mcp):
    services, gateway, _ = mcp
    assert "Uso: /mcp" in await command(services, "/mcp qualquer coisa")
    gateway.down = True
    assert "Não consegui falar com o gateway MCP" in await command(services, "/mcp")


async def test_status_has_an_mcp_line(mcp):
    services, gateway, _ = mcp
    lines = await collect_status(services)
    assert any("MCP: 1/2 servidores prontos, 2 ferramentas" in line for line in lines)
    gateway.down = True
    assert any("Gateway MCP" in line for line in await collect_status(services))


def test_log_filters_mcp_calls():
    from zoneinfo import ZoneInfo

    request = parse_log_args(["hoje", "mcp"], ZoneInfo("America/Sao_Paulo"))
    assert request.query.types == {EventType.MCP_CALL}


async def test_dashboard_mcp_tab(mcp):
    from uva.dashboard.app import STATIC, create_app

    services, _, _ = mcp
    action, params = services.actions.validate("mcp_brain_read_text_file", {"text": "a.md"})
    await execute_action(action, params, ActionContext(services))
    with TestClient(create_app(services=services)) as client:
        data = client.get("/api/mcp").json()
    assert data["enabled"] and data["error"] is None
    assert [s["name"] for s in data["servers"]] == ["brain", "github"]
    assert {t["action"] for t in data["tools"]} == {"mcp_brain_search_files", "mcp_brain_read_text_file"}
    assert data["calls"][0]["data"]["tool"] == "read_text_file"
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'data-tab="mcp"' in html and "/api/mcp" in html
