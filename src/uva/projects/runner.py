"""Builds a project: an empty git repo in a new folder, then Claude Code, headless.

Runs in the ``projects`` worker (its own container, see docker-compose.yml),
which mounts only the projects folder: no bot repository, no second brain.
Claude Code runs as a separate unprivileged user with a scrubbed environment
holding only its own credential, so it can't read the bot's secrets, and with a
closed list of permissions (ALLOWED_TOOLS), never full permissions.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from uva.clock import utcnow
from uva.logging import get_logger
from uva.logging.events import EventType, emit, trace
from uva.projects.store import ProjectRecord, ProjectStatus
from uva.replies import Reply
from uva.services import Services

log = get_logger(__name__)

Notify = Callable[[Reply], Awaitable[None]]
POLL_SECONDS = 3
SUMMARY_CHARS = 2500

PROMPT = """You are creating a brand-new software project, on your own, in the current directory
(an empty git repository whose commit author is already configured). Nobody will answer
questions: make reasonable decisions and keep going until it works.

Request (from the owner, in Portuguese):
{description}

Guidelines:
- Keep the scope to what was asked; prefer simple, well-known tools.
- Write a README.md in Portuguese: what it is, how to install, run and test it.
- Add automated tests where it makes sense and run them; fix what fails.
- Commit your work with git as you go, with clear messages (the author is configured).
- Everything stays inside this directory. Never print or commit secrets.
- Your shell is limited to git, python/python3, pip, node, npm, npx and pytest; other
  commands (ls, cat, mkdir, curl...) are refused: use your file tools instead (Read, Edit,
  Glob, Grep; writing a file creates its folders).
- When done, answer with a short summary in Portuguese (até ~12 linhas): what you built,
  how to run it and the test results."""


@dataclass
class Outcome:
    ok: bool
    summary: str = ""
    error: str | None = None
    cost_usd: float = 0.0
    turns: int = 0
    cancelled: bool = False


def _account(name: str) -> tuple[int, int, str] | None:
    """(uid, gid, home) to run Claude Code as, when we are root and the user exists."""
    if not name or not hasattr(os, "geteuid") or os.geteuid() != 0:
        return None
    import pwd  # POSIX only

    try:
        entry = pwd.getpwnam(name)
    except KeyError:
        return None
    return entry.pw_uid, entry.pw_gid, entry.pw_dir


def _child_env(services: Services, home: str) -> dict[str, str]:
    s = services.settings
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": home,
        "LANG": "C.UTF-8",
        "DISABLE_AUTOUPDATER": "1",
        "GIT_AUTHOR_NAME": s.git_author_name,
        "GIT_AUTHOR_EMAIL": s.git_author_email,
        "GIT_COMMITTER_NAME": s.git_author_name,
        "GIT_COMMITTER_EMAIL": s.git_author_email,
    }
    if os.name == "nt" and "SYSTEMROOT" in os.environ:  # local dev on Windows
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    if s.claude_code_oauth_token is not None:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = s.claude_code_oauth_token.get_secret_value()
    elif s.projects_anthropic_api_key is not None:
        env["ANTHROPIC_API_KEY"] = s.projects_anthropic_api_key.get_secret_value()
    return env


async def _git(cwd: Path, env: dict[str, str], account, *args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git", *args, cwd=cwd, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        **_as(account),
    )
    out, _ = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {out.decode(errors='replace').strip()[:300]}")
    return out.decode(errors="replace").strip()


def _as(account) -> dict:
    return {"user": account[0], "group": account[1]} if account else {}


# A closed list instead of full permissions: files only inside the project folder
# (the working directory), shell only for git, Python/pip, Node/npm/npx and pytest.
# In "dontAsk" mode anything else is refused, never asked.
ALLOWED_TOOLS = (
    "Read(./**)", "Edit(./**)", "Glob", "Grep", "TodoWrite",
    "Bash(git *)",
    "Bash(python *)", "Bash(python3 *)", "Bash(pip *)", "Bash(pip3 *)",
    "Bash(node *)", "Bash(npm *)", "Bash(npx *)",
    "Bash(pytest *)", "Bash(pytest)",
)
# Its own permission settings stay out of reach.
DISALLOWED_TOOLS = ("Edit(./.claude/**)",)


def claude_command(services: Services, prompt: str) -> list[str]:
    s = services.settings
    cmd = [
        s.project_claude_bin, "-p", prompt,
        "--output-format", "json",
        "--permission-mode", "dontAsk",
        "--allowedTools", *ALLOWED_TOOLS,
        "--disallowedTools", *DISALLOWED_TOOLS,
        "--max-turns", str(s.project_max_turns),
    ]
    if s.project_max_budget_usd > 0:
        cmd += ["--max-budget-usd", f"{s.project_max_budget_usd:g}"]
    if s.project_claude_model:
        cmd += ["--model", s.project_claude_model]
    return cmd


def parse_result(stdout: str) -> Outcome:
    """Claude Code's --output-format json: one object with result, cost and turns."""
    text = stdout.strip()
    data = None
    for candidate in (text, text.splitlines()[-1] if text else ""):
        try:
            data = json.loads(candidate)
            break
        except (json.JSONDecodeError, IndexError):
            continue
    if not isinstance(data, dict):
        return Outcome(False, error=f"saída inesperada do Claude Code: {text[-300:] or '(vazia)'}")
    cost = float(data.get("total_cost_usd") or 0)
    turns = int(data.get("num_turns") or 0)
    result = str(data.get("result") or "").strip()
    if data.get("is_error") or data.get("subtype") not in (None, "success"):
        reason = result or str(data.get("subtype"))
        return Outcome(False, summary=result, error=reason[:500], cost_usd=cost, turns=turns)
    return Outcome(True, summary=result, cost_usd=cost, turns=turns)


async def _run_claude(services: Services, record: ProjectRecord, cwd: Path, env, account) -> Outcome:
    s = services.settings
    store = services.projects.store  # type: ignore[union-attr]
    prompt = PROMPT.format(description=record.description)
    try:
        proc = await asyncio.create_subprocess_exec(
            *claude_command(services, prompt), cwd=cwd, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True, **_as(account),
        )
    except FileNotFoundError:
        return Outcome(False, error=f"Claude Code não está instalado ({s.project_claude_bin})")
    talk = asyncio.ensure_future(proc.communicate())
    deadline = time.monotonic() + s.project_timeout_minutes * 60
    stop_reason: str | None = None
    while True:
        done, _ = await asyncio.wait({talk}, timeout=POLL_SECONDS)
        if done:
            break
        current = await store.get(record.id)
        if current is not None and current.status is ProjectStatus.CANCELLING:
            stop_reason = "cancelado"
        elif time.monotonic() > deadline:
            stop_reason = f"passou de {s.project_timeout_minutes} min (PROJECT_TIMEOUT_MINUTES)"
        if stop_reason:
            _kill(proc)
            await talk
            break
    out, err = talk.result()
    if stop_reason:
        return Outcome(False, error=stop_reason, cancelled=stop_reason == "cancelado")
    outcome = parse_result(out.decode(errors="replace"))
    if not outcome.ok and proc.returncode and not outcome.summary:
        tail = err.decode(errors="replace").strip()[-300:]
        outcome.error = f"{outcome.error}; {tail}" if tail else outcome.error
    return outcome


def _kill(proc) -> None:
    try:
        if hasattr(os, "killpg"):
            os.killpg(proc.pid, 9)  # Claude Code and everything it started
        else:
            proc.kill()
    except ProcessLookupError:
        pass


async def build_project(services: Services, project_id: str, notify: Notify | None) -> ProjectRecord | None:
    projects = services.projects
    if projects is None:
        return None
    store = projects.store
    record = await store.get(project_id)
    if record is None or record.status is not ProjectStatus.QUEUED:
        return record  # cancelled before it started, or already handled
    with trace(record.trace_id):
        account = _account(services.settings.project_run_as_user)
        home = account[2] if account else os.environ.get("HOME", "/tmp")
        env = _child_env(services, home)
        cwd = projects.root / record.slug
        await store.update(project_id, status=ProjectStatus.RUNNING, started_at=utcnow())
        await emit(EventType.PROJECT, op="start", project_id=project_id, slug=record.slug)
        started = time.monotonic()
        try:
            cwd.mkdir(parents=True, exist_ok=False)
            if account:
                try:
                    shutil.chown(cwd, account[0], account[1])
                except OSError:  # some host bind mounts don't do ownership
                    cwd.chmod(0o777)
            await _git(cwd, env, account, "init", "-q", "-b", "main")
            await _git(cwd, env, account, "config", "user.name", services.settings.git_author_name)
            await _git(cwd, env, account, "config", "user.email", services.settings.git_author_email)
            outcome = await _run_claude(services, record, cwd, env, account)
            if not outcome.cancelled:
                await _commit_leftovers(cwd, env, account)
            commits = await _count_commits(cwd, env, account)
        except Exception as exc:
            log.exception("project_failed", project_id=project_id)
            outcome, commits = Outcome(False, error=repr(exc)[:500]), 0

        status = (
            ProjectStatus.CANCELLED if outcome.cancelled
            else ProjectStatus.DONE if outcome.ok else ProjectStatus.FAILED
        )
        fields = dict(
            status=status, summary=outcome.summary[:SUMMARY_CHARS], error=outcome.error,
            cost_usd=round(outcome.cost_usd, 4), turns=outcome.turns, commits=commits, finished_at=utcnow(),
        )
        await store.update(project_id, **fields)
        record = record.model_copy(update=fields)
        minutes = (time.monotonic() - started) / 60
        await emit(
            EventType.PROJECT, op="finish", project_id=project_id, status=status.value,
            cost_usd=fields["cost_usd"], turns=outcome.turns, commits=commits, minutes=round(minutes, 1),
            error=outcome.error,
        )
        if services.stm is not None:
            try:
                await services.stm.update_ref(
                    project_id, "ok" if outcome.ok else "falhou", outcome.error, focus=False
                )
            except Exception:
                pass
        if notify is not None:
            await notify(Reply(announce(record, projects.host_path(record.slug), minutes)))
        return record


async def _commit_leftovers(cwd: Path, env, account) -> None:
    if not await _git(cwd, env, account, "status", "--porcelain"):
        return
    await _git(cwd, env, account, "add", "-A")
    await _git(cwd, env, account, "commit", "-q", "-m", "Trabalho final do Claude Code")


async def _count_commits(cwd: Path, env, account) -> int:
    try:
        return int(await _git(cwd, env, account, "rev-list", "--count", "HEAD"))
    except (RuntimeError, ValueError):
        return 0  # no commit at all


def announce(record: ProjectRecord, host_path: str, minutes: float) -> str:
    facts = f"{minutes:.0f} min · {record.turns} turnos · US$ {record.cost_usd:.2f} · {record.commits} commit(s)"
    if record.status is ProjectStatus.DONE:
        return f"🚀 Projeto {record.slug} pronto ({record.id})\n📁 {host_path}\n{facts}\n\n{record.summary}"
    if record.status is ProjectStatus.CANCELLED:
        return f"🚫 Projeto {record.slug} cancelado ({record.id}). O que já tinha ficou em {host_path}."
    text = f"❌ Projeto {record.slug} falhou ({record.id}): {record.error}\n📁 {host_path}\n{facts}"
    if record.summary:
        text += f"\n\n{record.summary}"
    return text
