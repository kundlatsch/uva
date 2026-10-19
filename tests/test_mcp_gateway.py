"""The MCP gateway (uva/mcp/gateway.py) against a real tiny MCP server."""

from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from uva.mcp.config import McpConfigError, Policy, expand, load_config, parse_config
from uva.mcp.gateway import Gateway, GatewayError, create_app

ECHO = str(Path(__file__).parent / "fixtures" / "mcp_echo.py")


def write_config(tmp_path: Path, servers: dict) -> Path:
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    return path


def echo_server(**uva) -> dict:
    spec = {"type": "stdio", "command": sys.executable, "args": [ECHO]}
    if "env" in uva:
        spec["env"] = uva.pop("env")
    spec["uva"] = uva
    return spec


async def wait_for(gateway: Gateway, name: str, states=("ready",), timeout: float = 40) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if gateway.servers[name].state in states:
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"{name} is {gateway.servers[name].state}: {gateway.servers[name].error}")


@pytest.fixture
async def gateway(tmp_path):
    path = write_config(tmp_path, {"echo": echo_server(deny_tools=["wipe"])})
    gw = Gateway(path, {})
    await gw.start()
    await wait_for(gw, "echo")
    yield gw
    await gw.close()


# -- config --------------------------------------------------------------------


def test_policy_defaults_are_the_safe_ones():
    policy = Policy()
    assert policy.allows("anything")
    assert not policy.is_read("anything")
    assert not policy.is_read("anything", {"readOnlyHint": True})  # confirm_tools ["*"] wins over the hint
    assert policy.needs_confirmation("anything")
    assert policy.proactive_tools == []
    assert policy.timeout_s == 30 and policy.max_result_chars == 6000


def test_policy_read_tools_and_trusted_servers():
    policy = Policy(read_tools=["get_*"], confirm_tools=[], deny_tools=["delete-*"])
    assert policy.is_read("get_page")
    assert policy.is_read("search", {"readOnlyHint": True})
    assert not policy.needs_confirmation("create_page")
    assert not policy.allows("delete-page")


def test_config_parses_the_claude_desktop_shape(tmp_path):
    path = write_config(tmp_path, {
        "fs": {"command": "mcp-server-filesystem", "args": ["/mnt/brain"]},
        "gh": {"url": "https://example.com/mcp", "headers": {"Authorization": "Bearer ${PAT}"}},
    })
    specs = load_config(path)
    assert specs["fs"].type == "stdio" and specs["gh"].type == "http"
    assert specs["fs"].policy.enabled
    assert load_config(tmp_path / "missing.json") == {}


def test_config_errors():
    with pytest.raises(McpConfigError):
        parse_config({"mcpServers": {"x": {"type": "stdio"}}})
    with pytest.raises(McpConfigError):
        parse_config({"mcpServers": {"Bad Name": {"command": "x"}}})
    with pytest.raises(McpConfigError):
        parse_config({"mcpServers": {"x": {"command": "x", "uva": {"timeout_s": -1}}}})


def test_expand_reports_missing_variables():
    spec = parse_config({"mcpServers": {"gh": {"url": "https://x/mcp", "headers": {"Authorization": "Bearer ${PAT}"}}}})["gh"]
    expanded, missing = expand(spec, {"PAT": "abc"})
    assert expanded.headers["Authorization"] == "Bearer abc" and missing == []
    assert expand(spec, {"PAT": ""})[1] == ["PAT"]
    assert spec.headers["Authorization"] == "Bearer ${PAT}"  # the original is untouched


def test_repo_mcp_json_is_valid():
    specs = load_config(Path(__file__).parents[1] / "data" / "mcp.json")
    assert set(specs) == {"brain", "github"}
    assert specs["brain"].type == "stdio" and specs["github"].type == "http"
    assert not specs["brain"].policy.allows("write_file")
    assert not specs["github"].policy.allows("create_issue")
    assert specs["github"].policy.is_read("get_file_contents")
    assert expand(specs["github"], {})[1] == ["GITHUB_PAT"]


# -- stdio -----------------------------------------------------------------------


async def test_catalog_lists_allowed_tools_with_hashes(gateway):
    tools = {t["name"]: t for t in gateway.catalog()}
    assert {"echo", "add", "slow", "fail"} <= set(tools)
    assert "wipe" not in tools  # deny_tools
    echo = tools["echo"]
    assert echo["server"] == "echo"
    assert echo["input_schema"]["properties"]["text"]["type"] == "string"
    assert echo["annotations"]["readOnlyHint"] is True
    assert len(echo["hash"]) == 16
    assert gateway.list_servers()[0]["state"] == "ready"


async def test_call_returns_content_blocks(gateway):
    result = await gateway.call("echo", "echo", {"text": "oi"})
    assert result["content"] == [{"type": "text", "text": "echo: oi"}]
    assert result["is_error"] is False
    assert isinstance(result["duration_ms"], int)


async def test_tool_errors_come_back_as_is_error(gateway):
    result = await gateway.call("echo", "fail", {})
    assert result["is_error"] is True


async def test_call_timeout(gateway):
    with pytest.raises(GatewayError) as err:
        await gateway.call("echo", "slow", {"seconds": 5}, timeout_s=0.5)
    assert err.value.status == 504
    # The session still works afterwards.
    assert (await gateway.call("echo", "add", {"a": 1, "b": 2}))["content"][0]["text"] == "3"


async def test_blocked_and_unknown(gateway):
    with pytest.raises(GatewayError) as err:
        await gateway.call("echo", "wipe", {"target": "x"})
    assert err.value.status == 404
    with pytest.raises(GatewayError) as err:
        await gateway.call("nope", "echo", {})
    assert err.value.status == 404


async def test_changed_definition_changes_hash_and_version(tmp_path):
    path = write_config(tmp_path, {"echo": echo_server()})
    gw = Gateway(path, {})
    await gw.start()
    try:
        await wait_for(gw, "echo")
        before = {t["name"]: t["hash"] for t in gw.catalog()}
        version = gw.version()
        write_config(tmp_path, {"echo": echo_server(env={"ECHO_DESCRIPTION": "Now it does something else"})})
        await gw.reload()
        await wait_for(gw, "echo")
        after = {t["name"]: t["hash"] for t in gw.catalog()}
        assert before["echo"] != after["echo"]
        assert before["add"] == after["add"]
        assert gw.version() != version
    finally:
        await gw.close()


async def test_disabled_skipped_and_broken_servers(tmp_path):
    path = write_config(tmp_path, {
        "off": echo_server(enabled=False),
        "gh": {"url": "https://example.invalid/mcp", "headers": {"Authorization": "Bearer ${GITHUB_PAT}"}},
        "broken": {"command": "definitely-not-a-command-uva"},
    })
    gw = Gateway(path, {"GITHUB_PAT": ""}, backoff_min=0.1)
    await gw.start()
    try:
        assert gw.servers["off"].state == "disabled"
        assert gw.servers["gh"].state == "skipped"
        assert "GITHUB_PAT" in gw.servers["gh"].error
        await wait_for(gw, "broken", states=("error",))
        assert gw.servers["broken"].error
        assert gw.catalog() == []
        # It keeps retrying with backoff.
        deadline = time.monotonic() + 10
        while gw.servers["broken"].restarts < 2 and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        assert gw.servers["broken"].restarts >= 2
        # /mcp ligar: a runtime override, the file is untouched.
        await gw.set_enabled("off", True)
        await wait_for(gw, "off")
        assert any(t["server"] == "off" for t in gw.catalog())
        assert json.loads(path.read_text())["mcpServers"]["off"]["uva"]["enabled"] is False
    finally:
        await gw.close()


async def test_sampling_and_elicitation_are_not_offered(gateway):
    client = gateway.servers["echo"].client
    capabilities = client.session._build_capabilities(client.protocol_version)
    assert capabilities.sampling is None
    assert capabilities.elicitation is None
    assert capabilities.roots is None


async def test_invalid_config_keeps_running_servers(gateway):
    gateway.config_path.write_text("{not json", encoding="utf-8")
    result = await gateway.reload()
    assert result["ok"] is False and gateway.config_error
    assert gateway.servers["echo"].state == "ready"


# -- Streamable HTTP ---------------------------------------------------------------


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_streamable_http_server(tmp_path):
    port = free_port()
    proc = subprocess.Popen([sys.executable, ECHO, "http", str(port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    break
            except OSError:
                await asyncio.sleep(0.2)
        path = write_config(tmp_path, {
            "remote": {"type": "http", "url": f"http://127.0.0.1:{port}/mcp", "headers": {"X-Test": "${TOKEN}"}},
        })
        gw = Gateway(path, {"TOKEN": "t"})
        await gw.start()
        try:
            await wait_for(gw, "remote")
            result = await gw.call("remote", "echo", {"text": "http"})
            assert result["content"][0]["text"] == "echo: http"
        finally:
            await gw.close()
    finally:
        proc.terminate()
        proc.wait(timeout=10)


# -- HTTP API ------------------------------------------------------------------------


async def test_api_requires_the_token(gateway):
    app = create_app(gateway, "secret", manage=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://mcp") as client:
        assert (await client.get("/health")).status_code == 200
        assert (await client.get("/catalog")).status_code == 401
        assert (await client.get("/catalog", headers={"Authorization": "Bearer nope"})).status_code == 401
        auth = {"Authorization": "Bearer secret"}
        catalog = (await client.get("/catalog", headers=auth)).json()
        assert any(t["name"] == "echo" for t in catalog["tools"]) and catalog["version"]
        servers = (await client.get("/servers", headers=auth)).json()
        assert servers["servers"][0]["name"] == "echo"
        call = await client.post("/call", headers=auth, json={"server": "echo", "tool": "echo", "arguments": {"text": "x"}})
        assert call.json()["content"][0]["text"] == "echo: x"
        missing = await client.post("/call", headers=auth, json={"server": "echo", "tool": "wipe", "arguments": {}})
        assert missing.status_code == 404 and "wipe" in missing.json()["error"]


async def test_api_without_a_configured_token_refuses_everything(gateway):
    app = create_app(gateway, "", manage=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://mcp") as client:
        assert (await client.get("/catalog", headers={"Authorization": "Bearer "})).status_code == 401
