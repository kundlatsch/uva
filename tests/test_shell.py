import json
import os

import httpx
import pytest

from uva.router import Router
from uva.shell import executor
from uva.shell.guard import python_risks, shell_risks
from uva.shell.service import ShellService


class FakeSandbox:
    """Stands in for the executor's HTTP API."""

    def __init__(self, existing=()) -> None:
        self.existing = set(existing)
        self.runs: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        data = json.loads(request.content or b"{}")
        if request.url.path == "/exists":
            return httpx.Response(200, json={"exists": [p for p in data["paths"] if p in self.existing]})
        if request.url.path == "/run":
            self.runs.append(data)
            return httpx.Response(200, json={"exit": 0, "output": "ok\n", "seconds": 0.1, "snapshot": "abc1234"})
        if request.url.path == "/undo":
            return httpx.Response(200, json={"ok": True, "restored": "rm a.txt", "changed": ["D  a.txt"]})
        return httpx.Response(404, json={"error": "x"})


@pytest.fixture
def sandbox(services):
    fake = FakeSandbox(existing={"a.txt"})
    services.shell = ShellService(services.settings, transport=httpx.MockTransport(fake.handler))
    return fake


@pytest.mark.parametrize(
    "command",
    ["rm a.txt", "ls && rm -rf build", "find . -name '*.log' -delete", "git clean -fd", "mv a b",
     "truncate -s0 x", "ls | xargs rm", "sed -i s/a/b/ f", "python -c 'import os; os.remove(\"f\")'"],
)
def test_destructive_shell_commands(command):
    assert shell_risks(command)[0]


@pytest.mark.parametrize("command", ["ls -la", "echo oi >> log.txt", "cat a 2>&1", "pip install x", "python x.py"])
def test_harmless_shell_commands(command):
    assert shell_risks(command) == ([], [])


def test_overwrites_are_checked_against_existing_files():
    assert shell_risks("echo x > a.txt")[1] == ["a.txt"]
    assert shell_risks("cp src.txt dest.txt")[1] == ["dest.txt"]


def test_python_risks():
    assert python_risks("import shutil; shutil.rmtree('d')")
    assert python_risks("from pathlib import Path\nPath('a').unlink()")
    assert python_risks("open('a', 'w').write('x')")
    assert python_risks("print(sum(range(10)))") == []


async def test_harmless_command_runs_right_away(services, sandbox):
    reply = await Router(services).handle_text("/sh ls -la")
    assert sandbox.runs[0] == {"kind": "sh", "code": "ls -la", "label": "ls -la"}
    assert "ok" in reply.text and "snapshot abc1234" in reply.text


async def test_python_keeps_newlines(services, sandbox):
    await Router(services).handle_text("/py x = 2\nprint(x * 21)")
    assert sandbox.runs[0]["code"] == "x = 2\nprint(x * 21)"


async def test_delete_asks_and_runs_only_after_yes(services, sandbox):
    router = Router(services)
    reply = await router.handle_text("/sh rm a.txt")
    assert sandbox.runs == [] and "Rodo mesmo?" in reply.text
    yes, no = reply.buttons
    assert (await router.handle_callback(no.data)).text == "Ok, não rodei nada."
    assert sandbox.runs == []

    reply = await router.handle_text("/sh rm a.txt")
    await router.handle_callback(reply.buttons[0].data)
    assert sandbox.runs[0]["code"] == "rm a.txt"
    # A button works once.
    assert "expirou" in (await router.handle_callback(reply.buttons[0].data)).text


async def test_overwriting_an_existing_file_asks(services, sandbox):
    router = Router(services)
    assert "sobrescreve a.txt" in (await router.handle_text("/sh echo x > a.txt")).text
    await router.handle_text("/sh echo x > novo.txt")  # new file: no question
    assert sandbox.runs[0]["code"] == "echo x > novo.txt"


async def test_undo(services, sandbox):
    reply = await Router(services).handle_text("/sh desfazer")
    assert reply.text.startswith("↩️ /uva voltou para antes de: rm a.txt") and "D  a.txt" in reply.text


async def test_llm_action_also_needs_confirmation(services, sandbox):
    from fakes import with_llm

    plan = {"actions": [{"type": "shell_run", "params": {"command": "rm a.txt"}}], "reply": "Vou apagar."}
    with_llm(services, json.dumps(plan))
    reply = await Router(services).handle_text("apaga o a.txt da sua pasta")
    assert sandbox.runs == [] and [b.text for b in reply.buttons] == ["Sim, rodar", "Não"]


async def test_sandbox_down(services):
    def boom(request):
        raise httpx.ConnectError("no route")

    services.shell = ShellService(services.settings, transport=httpx.MockTransport(boom))
    assert "não respondeu" in (await Router(services).handle_text("/sh ls")).text


# --- the executor itself (Linux, as root: runs in the integration container) ---------

needs_root = pytest.mark.skipif(
    os.name != "posix" or not hasattr(os, "geteuid") or os.geteuid() != 0, reason="needs Linux root"
)


@needs_root
def test_executor_runs_as_user_snapshots_changes_and_undoes(tmp_path, monkeypatch):
    import pwd

    user = next(u.pw_name for u in pwd.getpwall() if u.pw_uid >= 1000 and u.pw_name != "nobody")
    monkeypatch.setattr(executor, "HOME", tmp_path / "uva")
    monkeypatch.setattr(executor, "SNAPSHOTS", tmp_path / "snap" / "repo.git")
    monkeypatch.setattr(executor, "USER", user)
    monkeypatch.setattr(executor, "_as_user_venv", None, raising=False)
    (tmp_path / "uva" / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / "uva" / ".venv" / "bin" / "python").symlink_to("/usr/local/bin/python")
    executor.setup()

    assert executor.run("sh", "id -un", "id")["output"].strip() == user
    assert executor.run("sh", "echo oi > a.txt", "cria")["snapshot"]
    assert executor.run("sh", "true", "nada")["snapshot"] is None
    assert executor.run("sh", "cat a.txt", "le")["snapshot"] is None  # nothing changed
    assert executor.run("sh", "rm a.txt", "apaga")["snapshot"]  # only change: a deletion
    assert executor.run("py", "print(6 * 7)", "py")["output"].strip() == "42"
    assert executor.run("sh", "touch /root/x 2>&1; echo $?", "fora")["output"].strip().endswith("1")

    undone = executor.undo()
    assert undone["restored"] == "apaga" and (tmp_path / "uva" / "a.txt").read_text() == "oi\n"
    undone = executor.undo()
    assert undone["restored"] == "cria" and not (tmp_path / "uva" / "a.txt").exists()
    assert executor.exists(["a.txt", "../etc/passwd", "/etc/passwd"]) == []


def test_shell_texts_in_english():
    from uva import i18n
    from uva.shell.service import USAGE, render_result, usage

    i18n.set_language("en")
    assert usage().startswith("Usage:\n/sh <command>")
    assert USAGE.startswith("Uso:")  # the constant stays Portuguese for old importers
    reply = render_result("sh", "ls", {"output": "", "exit": 1, "seconds": 0.1, "snapshot": "abc"}, 60)
    assert "(no output)" in reply.text and "❌ exited with code 1" in reply.text
    assert "changed files (snapshot abc; /sh undo)" in reply.text
    assert python_risks("import os\nos.remove('a')") == ["deletes files"]
