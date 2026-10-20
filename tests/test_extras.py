import json
from datetime import UTC, datetime

import pytest
from fakes import make_services, make_settings, with_llm
from fastapi.testclient import TestClient
from test_stm import InMemorySessionBackend

from uva import hooks
from uva.brain.vault import Vault
from uva.commands.extras import doctor
from uva.dashboard import app as dashboard
from uva.index.service import SearchIndex
from uva.initiatives import describe_status
from uva.router import Router
from uva.steering import SteerBox
from uva.storage.sqlite import Database
from uva.stm.service import ShortTermMemory


def plan(reply="", *actions) -> str:
    return json.dumps({"actions": [{"type": t, "params": p} for t, p in actions], "reply": reply})


@pytest.fixture
def ex(services, tmp_path):
    services.stm = ShortTermMemory(InMemorySessionBackend(), services.settings)
    services.settings.repo_path = str(tmp_path / "repo")
    (tmp_path / "repo" / "data").mkdir(parents=True)
    return services


async def test_aside_answers_with_context_and_leaves_nothing_behind(ex):
    provider, _ = with_llm(ex, plan("Lisboa fica em Portugal."), "Uns 20 graus.")
    router = Router(ex)
    await router.handle_text("vou pra Lisboa sexta")
    before = list((await ex.stm.session()).turns)
    reply = await router.handle_text("/aparte que temperatura faz lá?")
    assert reply.text == "↪️ Uns 20 graus."
    assert (await ex.stm.session()).turns == before  # not kept
    call = provider.calls[1]
    assert any("vou pra Lisboa" in m.content for m in call["messages"]) and "side question" in call["system"]


def test_steer_box():
    box = SteerBox()
    assert not box.offer("oi")  # closed: a turn of its own
    box.begin()
    assert not box.offer("/status") and box.offer("e o Porto?", "e o Porto?")
    assert box.take() == (["e o Porto?"], ["e o Porto?"]) and not box.open


async def test_a_message_between_lookup_rounds_joins_the_turn(ex):
    ex.steer = SteerBox()
    provider, _ = with_llm(ex, plan("", ("recall", {"query": "Lisboa"})), plan("Lisboa e Porto, anotado."))
    router = Router(ex)
    run = router.agent._run

    async def slow_lookup(action, params, ctx):
        assert ex.steer.offer("e o Porto também", "e o Porto também")  # he types meanwhile
        return await run(action, params, ctx)

    router.agent._run = slow_lookup
    reply = await router.handle_text("o que eu sei de Lisboa?")
    assert reply.text == "Lisboa e Porto, anotado."
    second = provider.calls[1]["messages"][-1].content
    assert "He added, while you were working" in second and "e o Porto também" in second
    user_turn = [t for t in (await ex.stm.session()).turns if t.role == "user"][-1]
    assert user_turn.text == "o que eu sei de Lisboa?\ne o Porto também"
    assert not ex.steer.open and not ex.steer.offer("depois")  # closed after the turn


async def test_cost_footer(ex):
    with_llm(ex, plan("Oi!"), plan("De novo."))
    router = Router(ex)
    assert "A partir de agora" in (await router.handle_text("/custo on")).text
    reply = await router.handle_text("me conta uma coisa")
    assert reply.text.startswith("Oi!") and "💲 150 tokens · US$" in reply.text
    await router.handle_text("/custo off")
    assert "💲" not in (await router.handle_text("outra coisa")).text


async def test_radar_goes_first_and_shows_in_proativo(ex):
    from test_initiatives import InMemoryInitiativeStore

    ex.initiatives = InMemoryInitiativeStore()
    assert "Sem radar ainda" in await describe_status(ex)
    (ex.settings.repo_dir / "data" / "radar.md").write_text("- preço do Steam Deck\n- vagas de Rust", encoding="utf-8")
    assert "📡 Radar (data/radar.md):\n- preço do Steam Deck" in await describe_status(ex)
    from uva.initiatives import _context

    context = await _context(ex, datetime(2026, 10, 19, 15, tzinfo=UTC))
    assert context.split("\n\n")[1].startswith("Radar (what he wants watched")


async def test_hooks(ex, monkeypatch):
    (ex.settings.repo_dir / "data" / "hooks.json").write_text(json.dumps({
        "backup": {"notice": "💾 Backup feito: {body}"},
        "nas": {"request": "vê o disco do NAS"},
    }), encoding="utf-8")
    sent = []

    async def notify(reply):
        sent.append(reply.text)

    assert await hooks.run_hook(ex, "backup", "ok, 3 GB", notify) == "notice"
    assert sent == ["💾 Backup feito: ok, 3 GB"]
    provider, _ = with_llm(ex, plan("Disco com 91%."))
    assert await hooks.run_hook(ex, "nas", '{"used": 91}', notify) == "request"
    assert sent[1] == "🪝 Webhook nas:\nDisco com 91%."
    assert '"used": 91' in provider.calls[0]["messages"][-1].content
    with pytest.raises(hooks.HookError):
        hooks.find(ex.settings, "nope")


def test_hook_endpoint(tmp_path, monkeypatch):
    settings = make_settings(dashboard_hook_token="s3cret", dashboard_password="pw", repo_path=str(tmp_path))
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "hooks.json").write_text('{"backup": {"notice": "ok {body}"}}', encoding="utf-8")
    queued = []

    async def fake_queue(name, body):
        queued.append((name, body))

    monkeypatch.setattr(dashboard, "queue_hook", fake_queue)
    with TestClient(dashboard.create_app(services=make_services(settings=settings))) as client:
        assert client.post("/hook/backup", content="x").status_code == 401
        assert client.post("/hook/backup", content="x", headers={"X-UVA-Token": "wrong"}).status_code == 401
        assert client.post("/hook/other", content="x", headers={"X-UVA-Token": "s3cret"}).status_code == 404
        ok = client.post("/hook/backup", content="feito", headers={"Authorization": "Bearer s3cret"})
        assert ok.status_code == 202 and queued == [("backup", "feito")]
        assert client.get("/api/env").status_code == 401  # the dashboard still wants its password


async def test_doctor(ex, tmp_path):
    repo = ex.settings.repo_dir
    (repo / ".env.example").write_text("TELEGRAM_BOT_TOKEN=\nLLM_API_KEY=\n", encoding="utf-8")
    (repo / ".env").write_text("TELEGRAM_BOT_TOKEN=x\nLLM_APY_KEY=typo\n", encoding="utf-8")

    async def down():
        return False

    ex.health_checks["Redis"] = down
    db = Database(tmp_path / "uva.db")
    db.migrate()
    ex.vault = Vault(tmp_path / "brain", ex.tz)
    ex.vault.ensure_structure()
    ex.vault.create_note("Uma nota", "texto", folder="inbox")
    ex.index = SearchIndex(db)  # not hooked to the vault: it misses the note
    (repo / "data" / "profile.md").write_text("x" * 5000, encoding="utf-8")
    text = "\n".join(await doctor(ex))
    assert "LLM_APY_KEY" in text and "Redis fora do ar" in text and "/reindexar" in text
    assert "profile.md passou do limite" in text and "/perfil consolidar" in text
    assert "disco (" in text
