"""Small git wrapper for the changes the bot makes to its own repository.

Every bot-made commit carries a trailer (UVA-Self-Edit / UVA-Self-Code) so
/reverter can find the last one and undo it without touching Gustavo's own
commits. Reverts carry "UVA-Reverts: <sha>" so the same commit is never
reverted twice.
"""

from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass
from pathlib import Path

SELF_EDIT = "UVA-Self-Edit"
SELF_CODE = "UVA-Self-Code"
REVERTS = "UVA-Reverts"
BOT_TRAILERS = (SELF_EDIT, SELF_CODE)

_FIELD, _RECORD = "\x1f", "\x1e"


class GitError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Commit:
    sha: str
    subject: str
    body: str
    parents: int

    def trailer(self, key: str) -> str | None:
        for line in self.body.splitlines():
            if line.startswith(f"{key}:"):
                return line.split(":", 1)[1].strip()
        return None

    @property
    def is_bot_commit(self) -> bool:
        return any(self.trailer(t) == "true" for t in BOT_TRAILERS)

    @property
    def kind(self) -> str:
        return "self-code" if self.trailer(SELF_CODE) == "true" else "self-edit"


class GitRepo:
    def __init__(self, path: Path, author_name: str, author_email: str) -> None:
        self.path = path
        self.author_name = author_name
        self.author_email = author_email
        self._lock = asyncio.Lock()  # one git operation at a time per process

    # --- low level -------------------------------------------------------------

    def run(self, *args: str, cwd: Path | None = None, check: bool = True) -> str:
        result = subprocess.run(
            [
                "git",
                "-c", f"user.name={self.author_name}",
                "-c", f"user.email={self.author_email}",
                "-c", "core.autocrlf=false",
                *args,
            ],
            cwd=cwd or self.path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
        )
        if check and result.returncode != 0:
            raise GitError(f"git {' '.join(args)}: {(result.stderr or result.stdout).strip()}")
        return result.stdout

    async def arun(self, *args: str, cwd: Path | None = None, check: bool = True) -> str:
        async with self._lock:
            return await asyncio.to_thread(self.run, *args, cwd=cwd, check=check)

    # --- operations ------------------------------------------------------------

    async def head(self) -> str:
        return (await self.arun("rev-parse", "HEAD")).strip()

    async def commit_paths(self, paths: list[str], subject: str, body: str, trailer: str) -> str:
        """Commit only ``paths`` (relative to the repo), leaving anything else alone."""
        message = "\n\n".join(p for p in (subject, body.strip(), f"{trailer}: true") if p)
        async with self._lock:
            await asyncio.to_thread(self.run, "add", "--", *paths)
            await asyncio.to_thread(self.run, "commit", "-m", message, "--", *paths)
            return (await asyncio.to_thread(self.run, "rev-parse", "HEAD")).strip()

    async def history(self, limit: int = 200) -> list[Commit]:
        out = await self.arun("log", f"-n{limit}", f"--format=%H{_FIELD}%P{_FIELD}%s{_FIELD}%B{_RECORD}")
        commits = []
        for record in out.split(_RECORD):
            record = record.strip("\n")
            if not record:
                continue
            sha, parents, subject, body = record.split(_FIELD, 3)
            commits.append(Commit(sha, subject, body, len(parents.split())))
        return commits

    async def last_bot_commit(self) -> Commit | None:
        """Most recent bot commit on the current branch that wasn't reverted yet."""
        commits = await self.history()
        reverted = {c.trailer(REVERTS) for c in commits if c.trailer(REVERTS)}
        for commit in commits:
            if commit.is_bot_commit and commit.sha not in reverted:
                return commit
        return None

    async def revert(self, commit: Commit) -> str:
        """Revert one commit with a marker trailer; aborts cleanly on conflict."""
        mainline = ["-m", "1"] if commit.parents > 1 else []
        message = f'Revert "{commit.subject}"\n\nUndone by /reverter.\n\n{REVERTS}: {commit.sha}'
        async with self._lock:
            try:
                await asyncio.to_thread(self.run, "revert", "--no-commit", *mainline, commit.sha)
                await asyncio.to_thread(self.run, "commit", "-m", message)
            except GitError:
                await asyncio.to_thread(self.run, "revert", "--abort", check=False)
                raise
            return (await asyncio.to_thread(self.run, "rev-parse", "HEAD")).strip()

    async def diff(self, commit: Commit) -> str:
        """The change ``commit`` made (what reverting it would undo)."""
        mainline = ["-m", "--first-parent"] if commit.parents > 1 else []
        return await self.arun("show", "--format=", "--stat", "--patch", *mainline, commit.sha)

    async def changed_files(self, sha: str) -> list[str]:
        out = await self.arun("show", "--name-only", "--format=", "-m", "--first-parent", sha)
        return [line for line in out.splitlines() if line.strip()]
