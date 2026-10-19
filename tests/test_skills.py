import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fakes import with_llm
from test_routines import InMemoryRoutineStore

from uva.actions.registry import ActionContext
from uva.llm.base import without_context
from uva.router import Router
from uva.routines import Every, Routine, run_due
from uva.selfmod.files import FileAccess
from uva.selfmod.git import GitRepo
from uva.skills import SkillError, SkillLibrary, parse
from uva.soul import Soul
from uva.storage.sqlite import Database

REPO_SKILLS = Path(__file__).parents[1] / "data" / "skills"

BODY = """## When to Use
When he asks for a weekly plan.

## Procedure
1. List the days.

## Pitfalls
Don't invent appointments.

## Verification
Seven days listed."""


def skill_text(name="weekly-plan", description="Plans the user's week", triggers=("planeja minha semana",)):
    meta = {"name": name, "description": description, "triggers": list(triggers), "version": 1}
    return f"---\n{json.dumps(meta, ensure_ascii=False)}\n---\n\n{BODY}\n"


def git(path, *args) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=path, capture_output=True, text=True, check=True,
    ).stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "data" / "skills" / "weekly-plan").mkdir(parents=True)
    (root / "data" / "skills" / "weekly-plan" / "SKILL.md").write_text(skill_text(), encoding="utf-8")
    (root / "data" / "soul.md").write_text("Sou irônica.", encoding="utf-8")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "data")
    git(root, "commit", "-q", "-m", "init")
    return root


@pytest.fixture
def sk_services(services, repo, tmp_path):
    db = Database(tmp_path / "uva.db")
    db.migrate()
    services.skills = SkillLibrary(repo / "data" / "skills", db)
    services.files = FileAccess(repo, None, GitRepo(repo, "kundlatsch", "k@example.com"))
    services.repo = services.files.git
    return services


def test_parse_validates_the_skill():
    assert parse(skill_text(), "weekly-plan").triggers == ("planeja minha semana",)
    with pytest.raises(SkillError, match="diferente da pasta"):
        parse(skill_text(), "other")
    with pytest.raises(SkillError, match="máximo 120"):
        parse(skill_text(description="x" * 121), "weekly-plan")
    with pytest.raises(SkillError, match="nome de habilidade inválido"):
        parse(skill_text(name="Weekly Plan"), "Weekly Plan")
    with pytest.raises(SkillError, match="## Procedure"):
        parse(skill_text().replace("## Procedure", "## Steps"), "weekly-plan")


@pytest.mark.skipif(not REPO_SKILLS.is_dir(), reason="data/skills is local (kept out of git)")
def test_seeded_skills_are_valid():
    library = SkillLibrary(REPO_SKILLS)
    names = {s.name for s in library.all()}
    assert {"reading-notes", "reflective-thoughts", "research-summary"} <= names


def test_library_reloads_when_the_file_changes(repo):
    library = SkillLibrary(repo / "data" / "skills")
    assert library.get("weekly-plan").description == "Plans the user's week"
    path = repo / "data" / "skills" / "weekly-plan" / "SKILL.md"
    path.write_text(skill_text(description="Plans the week, Monday first"), encoding="utf-8")
    import os

    stat = path.stat()
    os.utime(path, (stat.st_atime, stat.st_mtime + 5))
    assert library.get("weekly-plan").description == "Plans the week, Monday first"


async def test_index_goes_right_after_the_soul(sk_services, repo):
    provider, _ = with_llm(sk_services, json.dumps({"actions": [], "reply": "ok"}))
    soul = Soul(repo / "data" / "soul.md")
    soul.skills = sk_services.skills
    sk_services.llm.soul = soul
    await Router(sk_services).handle_text("qual a capital da França?")
    system = provider.calls[0]["system"]
    assert system.index("Sou irônica.") < system.index("## Your skills") < system.index("You are UVA")
    assert "- weekly-plan: Plans the user's week" in system
    assert "## Procedure" not in system  # only the index; the body is loaded on demand


async def test_trigger_attaches_the_skill_to_the_request(sk_services):
    provider, _ = with_llm(sk_services, json.dumps({"actions": [], "reply": "Seg: ..."}))
    await Router(sk_services).handle_text("planeja minha semana por favor, tenho dentista na terça")
    message = provider.calls[0]["messages"][-1].content
    assert "Skill to follow for this request: weekly-plan" in message and "List the days" in message
    assert without_context(message).startswith("planeja minha semana")
    usage = await sk_services.skills.usage()
    assert usage["weekly-plan"].uses == 1


async def test_unrelated_message_gets_no_skill(sk_services):
    provider, _ = with_llm(sk_services, json.dumps({"actions": [], "reply": "Paris."}))
    await Router(sk_services).handle_text("qual a capital da França?")
    assert "Skill to follow" not in provider.calls[0]["messages"][-1].content


async def test_skill_command_shows_or_runs(sk_services):
    router = Router(sk_services)
    shown = await router.handle_text("/skill weekly-plan")
    assert "## Procedure" in shown.text
    provider, _ = with_llm(sk_services, json.dumps({"actions": [], "reply": "feito"}))
    reply = await router.handle_text("/habilidade weekly-plan tenho prova na quinta")
    assert reply.text == "feito"
    message = provider.calls[0]["messages"][-1].content
    assert "weekly-plan" in message and without_context(message) == "tenho prova na quinta"
    listing = await router.handle_text("/habilidades")
    assert "weekly-plan: Plans the user's week · 1 uso(s)" in listing.text
    assert "não tenho" in (await router.handle_text("/skill nope")).text.lower()


async def test_create_is_versioned_outside_git(sk_services, repo):
    head = git(repo, "rev-parse", "HEAD")
    action = sk_services.actions.get("skill_create")
    params = action.params_model(name="grocery-list", description="Keeps the grocery list", body=BODY)
    result = await action.handler(params, ActionContext(sk_services))
    assert result.data["ok"] and "grocery-list" in result.text and result.data["commit"] is None
    assert sk_services.skills.get("grocery-list").description == "Keeps the grocery list"
    assert git(repo, "rev-parse", "HEAD") == head  # skills are kept out of git
    version = sk_services.files.last_version()
    assert version.path == "data/skills/grocery-list/SKILL.md" and version.backup is None
    again = await action.handler(params, ActionContext(sk_services))
    assert not again.data["ok"]
    bad = action.params_model(name="x-y", description="d", body="no sections")
    assert not (await action.handler(bad, ActionContext(sk_services))).data["ok"]


async def test_patch_needs_a_read_first(sk_services):
    ctx = ActionContext(sk_services)
    patch = sk_services.actions.get("skill_patch")
    params = patch.params_model(name="weekly-plan", old="1. List the days.", new="1. List the days, Monday first.")
    refused = await patch.handler(params, ctx)
    assert not refused.data["ok"] and "skill_read" in refused.text

    read = sk_services.actions.get("skill_read")
    assert "List the days" in (await read.handler(read.params_model(name="weekly-plan"), ctx)).text
    done = await patch.handler(params, ctx)
    assert done.data["ok"]
    skill = sk_services.skills.get("weekly-plan")
    assert "Monday first" in skill.body and skill.version == 2
    # The bot knows what it wrote: a second patch in a row works.
    again = patch.params_model(name="weekly-plan", old="Seven days listed.", new="Seven days, in order.")
    assert (await patch.handler(again, ctx)).data["ok"]
    missing = patch.params_model(name="weekly-plan", old="not there", new="x")
    assert not (await patch.handler(missing, ctx)).data["ok"]


async def test_reference_files(sk_services, repo):
    ctx = ActionContext(sk_services)
    write = sk_services.actions.get("skill_write_file")
    ok = await write.handler(write.params_model(name="weekly-plan", path="references/format.md", content="# Format"), ctx)
    assert ok.data["ok"]
    assert (repo / "data" / "skills" / "weekly-plan" / "references" / "format.md").exists()
    bad = await write.handler(write.params_model(name="weekly-plan", path="../soul.md", content="x"), ctx)
    assert not bad.data["ok"]
    read = sk_services.actions.get("skill_read")
    text = (await read.handler(read.params_model(name="weekly-plan", file="format.md"), ctx)).text
    assert text == "# Format"


async def test_routine_with_a_skill(sk_services):
    sk_services.routines = store = InMemoryRoutineStore()
    await store.add(Routine(id="r1", request="o que tenho essa semana?", every=Every.DAY, skill="weekly-plan",
                            next_run_at=datetime(2026, 1, 1, tzinfo=UTC)))
    provider, _ = with_llm(sk_services, json.dumps({"actions": [], "reply": "nada"}))
    sent = []

    async def notify(reply):
        sent.append(reply)

    await run_due(sk_services, notify)
    assert "Skill to follow for this request: weekly-plan" in provider.calls[0]["messages"][-1].content
    reply = await Router(sk_services).handle_text("/rotina r1 habilidade nenhuma")
    assert "não segue mais" in reply.text and store.items["r1"].skill == ""


async def test_reverter_undoes_skill_changes_from_data_versions(sk_services, repo):
    ctx = ActionContext(sk_services)
    read = sk_services.actions.get("skill_read")
    await read.handler(read.params_model(name="weekly-plan"), ctx)
    patch = sk_services.actions.get("skill_patch")
    await patch.handler(patch.params_model(name="weekly-plan", old="1. List the days.", new="1. Monday first."), ctx)
    assert "Monday first" in sk_services.skills.get("weekly-plan").body
    versions = list((repo / "data" / ".versions").glob("skills__weekly-plan__SKILL-*.md"))
    assert len(versions) == 1 and "1. List the days." in versions[0].read_text(encoding="utf-8")

    router = Router(sk_services)
    diff = (await router.handle_text("/reverter diff")).text
    assert "-1. Monday first." in diff and "+1. List the days." in diff
    reply = (await router.handle_text("/reverter")).text
    assert "data/skills/weekly-plan/SKILL.md voltou" in reply
    assert "1. List the days." in sk_services.skills.get("weekly-plan").body
    # Nothing else to undo from data/.versions/, and the undo itself is not undoable.
    assert sk_services.files.last_version() is None


async def test_reverter_removes_a_skill_it_created(sk_services, repo):
    action = sk_services.actions.get("skill_create")
    await action.handler(action.params_model(name="new-one", description="New", body=BODY), ActionContext(sk_services))
    reply = (await Router(sk_services).handle_text("/reverter")).text
    assert "Apaguei data/skills/new-one/SKILL.md" in reply
    assert not (repo / "data" / "skills" / "new-one").exists()


async def test_reverter_picks_the_newest_of_commit_and_file_change(sk_services, repo):
    action = sk_services.actions.get("skill_create")
    await action.handler(action.params_model(name="older", description="Older", body=BODY), ActionContext(sk_services))
    import time

    time.sleep(1.1)  # commit times have one-second resolution
    await sk_services.files.write("data/soul.md", "Sou gentil.", reason="teste")
    reply = (await Router(sk_services).handle_text("/reverter")).text
    assert reply.startswith("↩️ Revertido")  # the git commit is newer
    assert (repo / "data" / "skills" / "older" / "SKILL.md").exists()
