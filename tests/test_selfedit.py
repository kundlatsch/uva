import json
import subprocess

import pytest
from fakes import with_llm

from uva.router import Router
from uva.selfmod.files import FileAccess, FileAccessError
from uva.selfmod.git import SELF_EDIT, GitRepo


def git(path, *args) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=path, capture_output=True, text=True, check=True,
    ).stdout


@pytest.fixture
def repo_dir(tmp_path):
    root = tmp_path / "repo"
    (root / "data").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "data" / "soul.md").write_text("Sou irônica.\n", encoding="utf-8")
    (root / "src" / "security.py").write_text("SECRET = 1\n", encoding="utf-8")
    (root / ".env").write_text("TOKEN=x\n", encoding="utf-8")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "data", "src")
    git(root, "commit", "-q", "-m", "init")
    return root


@pytest.fixture
def files(repo_dir, tmp_path) -> FileAccess:
    brain = tmp_path / "brain"
    (brain / "00-inbox").mkdir(parents=True)
    (brain / "00-inbox" / "Ideia.md").write_text("ideia original", encoding="utf-8")
    return FileAccess(repo_dir, brain, GitRepo(repo_dir, "kundlatsch", "k@example.com"))


@pytest.mark.parametrize(
    "path",
    [
        "src/security.py",
        ".env",
        "docker-compose.yml",
        "data/../src/security.py",
        "data/../../etc/passwd",
        "/etc/passwd",
        "data/.hidden.md",
        "data/script.py",
        "soul.md",
        "mcp.env",
        "data/mcp.json",
        "data/MCP.json",
    ],
)
def test_paths_outside_the_allowlist_are_refused(files, path):
    with pytest.raises(FileAccessError):
        files.resolve(path)


async def test_write_in_data_commits_with_trailer(files, repo_dir):
    change = await files.write("data/soul.md", "Sou gentil.\n", reason="pedido do Gustavo")
    assert (repo_dir / "data" / "soul.md").read_text(encoding="utf-8") == "Sou gentil.\n"
    message = git(repo_dir, "log", "-1", "--format=%an|%B")
    assert message.startswith("kundlatsch|Self-edit: write data/soul.md")
    assert f"{SELF_EDIT}: true" in message and "pedido do Gustavo" in message
    assert change.commit == git(repo_dir, "rev-parse", "HEAD").strip()


async def test_commit_only_includes_the_edited_file(files, repo_dir):
    (repo_dir / "src" / "security.py").write_text("SECRET = 2\n", encoding="utf-8")  # unrelated WIP
    await files.append("data/soul.md", "Palavrão liberado.", reason="")
    changed = git(repo_dir, "show", "--name-only", "--format=", "HEAD").split()
    assert changed == ["data/soul.md"]
    assert "security.py" in git(repo_dir, "status", "--porcelain")


async def test_brain_notes_are_created_or_appended_never_overwritten(files):
    with pytest.raises(FileAccessError):
        await files.write("brain/00-inbox/Ideia.md", "apagado", reason="")
    created = await files.write("brain/00-inbox/Nova.md", "nova", reason="")
    assert created.commit is None  # the brain is not in git
    await files.append("brain/00-inbox/Ideia.md", "mais", reason="")
    assert files.read("brain/00-inbox/Ideia.md") == "ideia original\nmais"


async def test_revert_command_undoes_only_bot_commits(services, files, repo_dir):
    services.files, services.repo = files, files.git
    router = Router(services)
    assert "Não achei" in (await router.handle_text("/reverter")).text

    await files.write("data/soul.md", "v2\n", reason="")
    (repo_dir / "data" / "user.md").write_text("do gustavo", encoding="utf-8")
    git(repo_dir, "add", "data/user.md")
    git(repo_dir, "commit", "-q", "-m", "commit do gustavo")

    reply = (await router.handle_text("/reverter")).text
    assert reply.startswith("↩️ Revertido") and "Self-edit: write data/soul.md" in reply
    assert (repo_dir / "data" / "soul.md").read_text(encoding="utf-8") == "Sou irônica.\n"
    assert (repo_dir / "data" / "user.md").exists()  # the user's commit is untouched
    # Nothing left to revert: the revert itself is not a bot change.
    assert "Não achei" in (await router.handle_text("/reverter")).text


async def test_llm_edits_soul_through_actions(services, files, repo_dir):
    services.files, services.repo = files, files.git
    plan = {
        "actions": [
            {
                "type": "file_write",
                "params": {"path": "data/soul.md", "content": "Mais educada.\n", "reason": "pedido"},
            }
        ],
        "reply": "Tá bom, vou pegar leve.",
    }
    with_llm(services, json.dumps(plan))
    reply = await Router(services).handle_text("fica mais educada")
    assert "✏️ data/soul.md atualizado (commit" in reply.text
    assert (repo_dir / "data" / "soul.md").read_text(encoding="utf-8") == "Mais educada.\n"


async def test_llm_cannot_touch_forbidden_files(services, files, repo_dir):
    services.files, services.repo = files, files.git
    plan = {"actions": [{"type": "file_write", "params": {"path": ".env", "content": "x"}}], "reply": ""}
    with_llm(services, json.dumps(plan))
    reply = await Router(services).handle_text("muda o .env")
    assert "Não mexi em .env" in reply.text
    assert (repo_dir / ".env").read_text(encoding="utf-8") == "TOKEN=x\n"
