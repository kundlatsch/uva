import json
import re

import pytest
from fakes import make_settings, with_llm
from fastapi.testclient import TestClient
from pydantic import SecretStr

from uva import i18n
from uva.dashboard.app import STATIC, create_app, mask_settings


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


def final(response) -> dict:
    lines = [json.loads(line) for line in response.text.splitlines() if line.strip()]
    assert lines[-1]["type"] == "final", lines
    return lines[-1]


@pytest.fixture
def client(services):
    with TestClient(create_app(services=services)) as c:
        yield c


def test_page_and_env(client):
    assert "UVA" in client.get("/").text
    names = {r["name"] for r in client.get("/api/env").json()}
    assert "TELEGRAM_BOT_TOKEN" in names


def test_page_texts_in_both_languages(client):
    pt = client.get("/api/i18n").json()
    assert pt["lang"] == "pt"
    assert pt["texts"]["overview.no_errors"] == "Nenhum erro nas últimas 24h. 🎉"
    assert pt["texts"]["tabs.overview"] == "Visão geral"
    i18n.set_language("en")
    en = client.get("/api/i18n").json()
    assert en["lang"] == "en"
    assert en["texts"]["overview.no_errors"] == "No errors in the last 24h. 🎉"
    assert en["texts"]["logs.count"] == "{n} events"  # placeholders are filled by the page
    assert set(en["texts"]) == set(pt["texts"])
    assert not any(k.startswith("dashboard.") for k in en["texts"])


def test_every_text_the_page_uses_exists(client):
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    used = set(re.findall(r'data-i18n(?:-title|-placeholder)?="([^"]+)"', page))
    used |= set(re.findall(r'\bt\("([\w.]+)"', page))
    assert len(used) > 50
    texts = client.get("/api/i18n").json()["texts"]
    assert sorted(used - set(texts)) == []


def test_chat_goes_through_the_router_with_debug(client, services):
    plan = {"actions": [], "reply": "Oi, Taguma!"}
    with_llm(services, json.dumps(plan))
    data = final(client.post("/api/chat", json={"text": "me conta algo legal sobre você"}))
    assert data["reply"]["text"] == "Oi, Taguma!"
    debug = data["debug"]
    assert [c["purpose"] for c in debug["llm_calls"]] == ["plan"]
    assert "ALWAYS answer with ONLY a JSON object" in debug["llm_calls"][0]["system"]
    assert any(e["type"] == "route" and e["data"].get("tier") == "llm" for e in debug["events"])


def test_commands_and_buttons_work(client):
    assert "pong" in final(client.post("/api/chat", json={"text": "/ping"}))["reply"]["text"].lower()
    expired = final(client.post("/api/callback", json={"data": "confirm:nope"}))
    assert expired["reply"]["text"]


def test_password(services):
    services.settings.dashboard_password = SecretStr("s3nha")
    with TestClient(create_app(services=services)) as c:
        assert c.get("/api/env").status_code == 401
        assert c.get("/api/i18n").status_code == 401
        assert c.get("/api/env", auth=("x", "errada")).status_code == 401
        assert c.get("/api/env", auth=("gustavo", "s3nha")).status_code == 200


def test_slow_chat_streams_a_placeholder_first(client, services, monkeypatch):
    from uva import progress

    monkeypatch.setattr(progress, "PLACEHOLDER_AFTER", 0.01)
    with_llm(services, json.dumps({"actions": [], "reply": "Demorei, mas cheguei."}))
    original = services.llm.complete

    async def slow(*args, **kwargs):
        import asyncio

        await asyncio.sleep(0.1)
        return await original(*args, **kwargs)

    services.llm.complete = slow
    lines = [json.loads(x) for x in client.post("/api/chat", json={"text": "fala algo"}).text.splitlines() if x.strip()]
    assert lines[0]["type"] == "post" and lines[0]["text"]  # a ready-made phrase first
    assert any(x["type"] == "edit" and x["text"] == "Demorei, mas cheguei." for x in lines)
    assert lines[-1]["type"] == "final"
