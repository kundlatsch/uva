import subprocess
import sys
from pathlib import Path

import pytest
from fakes import with_llm
from pydantic import SecretStr

from uva.projects import runner
from uva.projects.runner import build_project, parse_result
from uva.projects.service import ProjectService, slugify
from uva.projects.store import ProjectStatus
from uva.router import Router

FAKE_CLAUDE = r'''
import json, os, pathlib, subprocess, sys, time
mode = os.environ.get("FAKE_MODE", "ok")
if mode == "slow":
    time.sleep(60)
pathlib.Path("README.md").write_text("# Receitas\n", encoding="utf-8")
subprocess.run(["git", "add", "-A"], check=True)
subprocess.run(["git", "commit", "-q", "-m", "Primeira versão"], check=True)
pathlib.Path("app.py").write_text("print('oi')\n", encoding="utf-8")  # left uncommitted
secret_seen = "TELEGRAM_BOT_TOKEN" in os.environ or "LLM_API_KEY" in os.environ
print(json.dumps({
    "type": "result", "subtype": "success", "is_error": mode == "error",
    "result": "Criei um site de receitas. Rode com python app.py. 3 testes passando."
              + (" SEGREDO VISTO" if secret_seen else ""),
    "total_cost_usd": 0.42, "num_turns": 7,
}))
'''


class InMemoryProjectStore:
    def __init__(self) -> None:
        self.records = {}

    async def add(self, record):
        self.records[record.id] = record

    async def get(self, project_id):
        return self.records.get(project_id)

    async def update(self, project_id, **fields):
        self.records[project_id] = self.records[project_id].model_copy(update=fields)

    async def recent(self, limit):
        return sorted(self.records.values(), key=lambda r: r.created_at, reverse=True)[:limit]

    async def cost_since(self, since):
        return sum(r.cost_usd for r in self.records.values() if r.created_at >= since)


class Inbox:
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def __call__(self, reply) -> None:
        self.texts.append(reply.text)


@pytest.fixture
def proj(services, tmp_path, monkeypatch):
    s = services.settings
    s.projects_path = str(tmp_path / "projects")
    s.projects_host_path = "D:/uva-projects"
    s.claude_code_oauth_token = SecretStr("tok")
    enqueued: list[str] = []

    async def enqueue(project_id):
        enqueued.append(project_id)

    services.projects = ProjectService(InMemoryProjectStore(), s, enqueue)
    fake = tmp_path / "fake_claude.py"
    fake.write_text(FAKE_CLAUDE, encoding="utf-8")
    monkeypatch.setattr(runner, "claude_command", lambda services, prompt: [sys.executable, str(fake), "-p", prompt])
    monkeypatch.setattr(runner, "POLL_SECONDS", 0.2)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "must-not-leak")
    return enqueued


def git(path: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=path, capture_output=True, text=True, encoding="utf-8", check=True).stdout.strip()


def test_slugify():
    assert slugify("cria um site de receitas com Flask e busca") == "site-receitas-flask-busca"
    assert slugify("faz um projeto") == "projeto"


async def test_projeto_command_queues_a_project(services, proj):
    reply = (await Router(services).handle_text("/projeto um site estático de receitas com busca")).text
    assert reply.startswith("🏗️ Beleza, vou criar o projeto site-estatico-receitas-busca")
    assert "D:/uva-projects/site-estatico-receitas-busca" in reply and len(proj) == 1
    assert "⏳ na fila" in (await Router(services).handle_text("/projetos")).text
    # Only one at a time.
    assert "Já tem um projeto em construção" in (await Router(services).handle_text("/projeto outro app de notas")).text


async def test_missing_credential_is_explained(services, proj):
    services.settings.claude_code_oauth_token = None
    reply = (await Router(services).handle_text("/projeto um site estático de receitas")).text
    assert "CLAUDE_CODE_OAUTH_TOKEN" in reply and "claude setup-token" in reply and proj == []


async def test_llm_can_create_a_project(services, proj):
    import json

    plan = {"actions": [{"type": "project_create", "params": {"description": "um app de lista de compras em Python"}}],
            "reply": ""}
    with_llm(services, json.dumps(plan))
    reply = (await Router(services).handle_text("cria um projeto novo de lista de compras")).text
    assert "projeto app-lista-compras-python" in reply and len(proj) == 1


async def test_build_runs_claude_in_a_new_git_repo_and_announces(services, proj, event_store):
    record = await services.projects.request("um site de receitas", None, services.tz)
    inbox = Inbox()
    final = await build_project(services, record.id, inbox)

    assert final.status is ProjectStatus.DONE and final.cost_usd == 0.42 and final.turns == 7
    folder = Path(services.settings.projects_path) / record.slug
    assert (folder / "README.md").exists()
    assert git(folder, "log", "--format=%an <%ae>|%s").splitlines() == [
        "kundlatsch <gustavo.kundlatsch@gmail.com>|Trabalho final do Claude Code",
        "kundlatsch <gustavo.kundlatsch@gmail.com>|Primeira versão",
    ]
    assert final.commits == 2
    assert inbox.texts[0].startswith(f"🚀 Projeto {record.slug} pronto")
    assert "D:/uva-projects/" in inbox.texts[0] and "US$ 0.42" in inbox.texts[0]
    assert "SEGREDO VISTO" not in final.summary  # the bot's secrets never reach Claude Code
    assert {e.data["op"] for e in event_store.of_type("project")} >= {"start", "finish"}


async def test_failure_is_reported(services, proj, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "error")
    monkeypatch.setattr(runner, "_child_env", lambda s, h: {**runner.os.environ})
    record = await services.projects.request("um site de receitas", None, services.tz)
    inbox = Inbox()
    final = await build_project(services, record.id, inbox)
    assert final.status is ProjectStatus.FAILED and inbox.texts[0].startswith("❌ Projeto")


async def test_cancel_stops_a_running_project(services, proj, monkeypatch):
    monkeypatch.setattr(runner, "_child_env", lambda s, h: {**runner.os.environ, "FAKE_MODE": "slow"})
    record = await services.projects.request("um site de receitas", None, services.tz)
    original_get = services.projects.store.get
    calls = {"n": 0}

    async def get(project_id):
        calls["n"] += 1
        if calls["n"] == 3:  # while Claude Code is running
            await services.projects.cancel(project_id)
        return await original_get(project_id)

    services.projects.store.get = get
    inbox = Inbox()
    final = await build_project(services, record.id, inbox)
    assert final.status is ProjectStatus.CANCELLED and inbox.texts[0].startswith("🚫 Projeto")


async def test_cancelar_command_on_a_queued_project(services, proj):
    record = await services.projects.request("um site de receitas", None, services.tz)
    reply = (await Router(services).handle_text(f"/cancelar {record.id}")).text
    assert "Parando o projeto" in reply
    assert services.projects.store.records[record.id].status is ProjectStatus.CANCELLED
    # The worker then skips it.
    assert (await build_project(services, record.id, None)).status is ProjectStatus.CANCELLED


async def test_daily_budget(services, proj):
    record = await services.projects.request("um site de receitas", None, services.tz)
    await services.projects.store.update(record.id, status=ProjectStatus.DONE, cost_usd=10.0)
    reply = (await Router(services).handle_text("/projeto outro site de receitas")).text
    assert "Orçamento diário de projetos esgotado" in reply


def test_parse_result_handles_garbage():
    assert not parse_result("Error: not logged in").ok
    assert parse_result('{"subtype": "error_max_turns", "is_error": true, "total_cost_usd": 1}').error


def test_projects_task_goes_to_its_own_queue():
    from uva.tasks.app import app

    assert app.conf.task_routes["uva.project_build"] == {"queue": "projects"}


def test_claude_runs_with_a_closed_permission_list(services):
    cmd = runner.claude_command(services, "x")
    assert "bypassPermissions" not in cmd and cmd[cmd.index("--permission-mode") + 1] == "dontAsk"
    allowed = cmd[cmd.index("--allowedTools") + 1 : cmd.index("--disallowedTools")]
    assert "Bash(git *)" in allowed and "Edit(./**)" in allowed
    assert all(not a.startswith("Bash") or a.split("(")[1].split()[0].rstrip(")") in
               {"git", "python", "python3", "pip", "pip3", "node", "npm", "npx", "pytest"} for a in allowed)
