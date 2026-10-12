import json

import pytest
from fakes import make_settings, with_llm
from fastapi.testclient import TestClient
from pydantic import SecretStr

from uva.dashboard.app import create_app, mask_settings


def test_secrets_are_masked():
    settings = make_settings(
        llm_api_key="sk-supersecret", mongo_uri="mongodb://uva:hunter2@mongo:27017/uva",
        claude_code_oauth_token="sk-ant-oat01-xyz",
    )
    rows = {r["name"]: r for r in mask_settings(settings)}
    dump = json.dumps(rows)
    assert "supersecret" not in dump and "hunter2" not in dump and "oat01-xyz" not in dump and "123:abc" not in dump
    assert rows["LLM_API_KEY"]["value"] == "•••••• (definido)" and rows["LLM_API_KEY"]["secret"]
    assert rows["MONGO_URI"]["value"] == "mongodb://uva:••••••@mongo:27017/uva"
    assert rows["TIMEZONE"]["value"] == "America/Sao_Paulo"


@pytest.fixture
def client(services):
    with TestClient(create_app(services=services)) as c:
        yield c


def test_page_and_env(client):
    assert "UVA" in client.get("/").text
    names = {r["name"] for r in client.get("/api/env").json()}
    assert "TELEGRAM_BOT_TOKEN" in names


def test_chat_goes_through_the_router_with_debug(client, services):
    plan = {"actions": [], "reply": "Oi, Taguma!"}
    with_llm(services, json.dumps(plan))
    data = client.post("/api/chat", json={"text": "me conta algo legal sobre você"}).json()
    assert data["reply"]["text"] == "Oi, Taguma!"
    debug = data["debug"]
    assert [c["purpose"] for c in debug["llm_calls"]] == ["plan"]
    assert "ALWAYS answer with ONLY a JSON object" in debug["llm_calls"][0]["system"]
    assert any(e["type"] == "route" and e["data"].get("tier") == "llm" for e in debug["events"])


def test_commands_and_buttons_work(client):
    assert "pong" in client.post("/api/chat", json={"text": "/ping"}).json()["reply"]["text"].lower()
    expired = client.post("/api/callback", json={"data": "confirm:nope"}).json()
    assert expired["reply"]["text"]


def test_password(services):
    services.settings.dashboard_password = SecretStr("s3nha")
    with TestClient(create_app(services=services)) as c:
        assert c.get("/api/env").status_code == 401
        assert c.get("/api/env", auth=("x", "errada")).status_code == 401
        assert c.get("/api/env", auth=("gustavo", "s3nha")).status_code == 200
