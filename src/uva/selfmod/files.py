"""Files the bot may read and change by itself, and nothing else.

Allowed roots:
  * ``data/...``  -> <repo>/data (soul.md, fallback_phrases.json, prompts): read,
                     overwrite, append (.md/.txt); every change becomes a git
                     commit (UVA-Self-Edit). JSON files are validated before
                     they are written and can't be appended to. Files kept
                     out of git (data/profile.md, data/skills/) get the
                     previous version in data/.versions/ instead, listed in
                     data/.versions/journal.jsonl so /reverter can restore it.
  * ``brain/...`` -> the second brain: read, create new notes, append;
                     existing notes are never overwritten (and not in git).
Everything else (src/, security.py, .env, mcp.env, docker-compose.yml...) is refused,
and so is data/mcp.json (MCP servers and their policy: only Gustavo edits it).
"""

from __future__ import annotations

import difflib
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from uva.brain.vault import PlaceholderError, check_no_placeholders
from uva.i18n import t
from uva.phrases import PhrasesError
from uva.phrases import validate as validate_phrases
from uva.selfmod.git import SELF_EDIT, GitRepo

ALLOWED_SUFFIXES = {".md", ".txt"}
DATA_ONLY_SUFFIXES = {".json"}
MAX_BYTES = 50_000
# Files kept out of git (see .gitignore): edits keep a copy in data/.versions/.
UNTRACKED = {"data/profile.md"}
UNTRACKED_PREFIXES = ("data/skills/",)
JOURNAL = "journal.jsonl"
# Under data/ but never read nor written by the bot (configured by hand).
PROTECTED = {"data/mcp.json"}


def is_untracked(repo_relative: str | None) -> bool:
    return repo_relative is not None and (
        repo_relative in UNTRACKED or repo_relative.startswith(UNTRACKED_PREFIXES)
    )


@dataclass(frozen=True, slots=True)
class FileVersion:
    """One change to a file kept out of git (a line of the versions journal)."""

    id: str
    path: str  # "data/skills/x/SKILL.md"
    backup: str | None  # file name in data/.versions/, None when the change created the file
    at: datetime
    reason: str = ""


class FileAccessError(PermissionError):
    pass


@dataclass(frozen=True, slots=True)
class Target:
    root: str  # "data" | "brain"
    path: Path  # absolute
    display: str  # e.g. "data/soul.md"
    repo_relative: str | None  # path inside the git repo, for commits


@dataclass(frozen=True, slots=True)
class Change:
    target: Target
    commit: str | None


class FileAccess:
    def __init__(
        self,
        repo_dir: Path,
        brain_dir: Path | None,
        git: GitRepo | None,
        caps: dict[str, int] | None = None,
    ) -> None:
        self.roots: dict[str, Path] = {"data": repo_dir / "data"}
        if brain_dir is not None:
            self.roots["brain"] = brain_dir
        self.repo_dir = repo_dir
        self.git = git
        # Max characters per file ("data/profile.md": 4000): always-loaded files.
        self.caps = caps or {}

    def resolve(self, raw: str) -> Target:
        rel = PurePosixPath(raw.strip().replace("\\", "/"))
        parts = rel.parts
        if rel.is_absolute() or len(parts) < 2 or any(p in ("", ".", "..") for p in parts):
            raise FileAccessError(t("selfmod.files.bad_path", path=raw))
        if any(p.startswith(".") for p in parts):
            raise FileAccessError(t("selfmod.files.hidden", path=raw))
        root_name = parts[0]
        if rel.as_posix().lower() in PROTECTED:
            raise FileAccessError(t("selfmod.files.protected", path=raw))
        if root_name not in self.roots:
            allowed = ", ".join(f"{r}/" for r in self.roots)
            raise FileAccessError(t("selfmod.files.roots", allowed=allowed, path=raw))
        suffix = rel.suffix.lower()
        if suffix not in ALLOWED_SUFFIXES and not (root_name == "data" and suffix in DATA_ONLY_SUFFIXES):
            raise FileAccessError(t("selfmod.files.suffix", path=raw))

        root = self.roots[root_name].resolve()
        path = (root / PurePosixPath(*parts[1:])).resolve()
        if not path.is_relative_to(root):
            raise FileAccessError(t("selfmod.files.bad_path", path=raw))
        repo_relative = None
        if root_name == "data":
            repo_relative = path.relative_to(self.repo_dir.resolve()).as_posix()
        return Target(root_name, path, rel.as_posix(), repo_relative)

    def read(self, raw: str) -> str:
        target = self.resolve(raw)
        if not target.path.is_file():
            raise FileNotFoundError(target.display)
        return target.path.read_text(encoding="utf-8")

    async def write(self, raw: str, content: str, reason: str) -> Change:
        target = self.resolve(raw)
        _check_size(content)
        self._check_cap(target, content)
        if target.root == "brain" and target.path.exists():
            raise FileAccessError(t("selfmod.files.no_overwrite"))
        if target.path.suffix == ".json":
            _validate_json(target, content)
        try:
            check_no_placeholders(content)
        except PlaceholderError as exc:
            raise FileAccessError(str(exc)) from exc
        target.path.parent.mkdir(parents=True, exist_ok=True)
        self._backup_untracked(target, reason)
        target.path.write_text(content, encoding="utf-8")
        return Change(target, await self._commit(target, "write", reason))

    async def append(self, raw: str, content: str, reason: str) -> Change:
        target = self.resolve(raw)
        if target.path.suffix == ".json":
            raise FileAccessError(t("selfmod.files.no_json_append"))
        existing = target.path.read_text(encoding="utf-8") if target.path.exists() else ""
        _check_size(existing + content)
        self._check_cap(target, existing + content)
        target.path.parent.mkdir(parents=True, exist_ok=True)
        self._backup_untracked(target, reason)
        with target.path.open("a", encoding="utf-8") as fh:
            fh.write(("\n" if existing and not existing.endswith("\n") else "") + content)
        return Change(target, await self._commit(target, "append", reason))

    def _check_cap(self, target: Target, content: str) -> None:
        """Always-loaded files have a budget: refuse a write that goes over it,
        saying the size and asking to consolidate instead of growing."""
        limit = self.caps.get(target.display)
        if limit is not None and len(content) > limit:
            raise FileAccessError(
                t("selfmod.files.over_cap", path=target.display, size=len(content), limit=limit)
            )

    @property
    def versions_dir(self) -> Path:
        return self.roots["data"] / ".versions"

    def _backup_untracked(self, target: Target, reason: str = "") -> None:
        """Files outside git: keep the old version and note the change in the journal."""
        if not is_untracked(target.repo_relative):
            return
        versions = self.versions_dir
        versions.mkdir(exist_ok=True)
        backup = None
        if target.path.exists():
            # data/profile.md -> profile-<stamp>.md; data/skills/x/SKILL.md -> skills__x__SKILL-<stamp>.md
            flat = PurePosixPath(target.repo_relative.removeprefix("data/"))
            stem = "__".join([*flat.parent.parts, flat.stem]) if flat.parent.parts else flat.stem
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            path = versions / f"{stem}-{stamp}{flat.suffix}"
            counter = 2
            while path.exists():
                path = versions / f"{stem}-{stamp}-{counter}{flat.suffix}"
                counter += 1
            path.write_text(target.path.read_text(encoding="utf-8"), encoding="utf-8")
            backup = path.name
        entry = {"id": secrets.token_hex(3), "path": target.display, "backup": backup,
                 "at": datetime.now(UTC).isoformat(), "reason": reason[:200]}
        with (versions / JOURNAL).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # --- undoing changes to files kept out of git (/reverter) ----------------------

    def _journal(self) -> tuple[list[dict], set[str]]:
        path = self.versions_dir / JOURNAL
        if not path.exists():
            return [], set()
        entries, reverted = [], set()
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if "reverts" in item:
                reverted.add(item["reverts"])
            elif "id" in item:
                entries.append(item)
        return entries, reverted

    def last_version(self) -> FileVersion | None:
        """The most recent change to an untracked file that wasn't undone yet."""
        entries, reverted = self._journal()
        for item in reversed(entries):
            if item["id"] not in reverted:
                return FileVersion(item["id"], item["path"], item.get("backup"),
                                   datetime.fromisoformat(item["at"]), item.get("reason", ""))
        return None

    def version_diff(self, version: FileVersion) -> str:
        """What undoing ``version`` would change (unified diff, current -> previous)."""
        target = self.resolve(version.path)
        current = target.path.read_text(encoding="utf-8") if target.path.exists() else ""
        previous = (self.versions_dir / version.backup).read_text(encoding="utf-8") if version.backup else ""
        return "".join(difflib.unified_diff(
            current.splitlines(keepends=True), previous.splitlines(keepends=True),
            fromfile=f"{version.path} (now)", tofile=f"{version.path} (after undo)",
        ))

    def undo_version(self, version: FileVersion) -> None:
        """Put the previous version back (or remove a file the change created)."""
        target = self.resolve(version.path)
        if version.backup:
            content = (self.versions_dir / version.backup).read_text(encoding="utf-8")
            self._backup_untracked(target, reason=f"undo {version.id}")
            target.path.write_text(content, encoding="utf-8")
            # The backup made just now is the undo itself, not a change to undo.
            entries, _ = self._journal()
            self._mark_reverted(entries[-1]["id"])
        elif target.path.exists():
            target.path.unlink()
            parent = target.path.parent
            if parent != self.roots["data"] and not any(parent.iterdir()):
                parent.rmdir()
        self._mark_reverted(version.id)

    def _mark_reverted(self, entry_id: str) -> None:
        with (self.versions_dir / JOURNAL).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"reverts": entry_id, "at": datetime.now(UTC).isoformat()}) + "\n")

    async def _commit(self, target: Target, op: str, reason: str) -> str | None:
        if target.repo_relative is None or self.git is None or is_untracked(target.repo_relative):
            return None
        return await self.git.commit_paths(
            [target.repo_relative],
            subject=f"Self-edit: {op} {target.display}",
            body=reason or "Change made by UVA.",
            trailer=SELF_EDIT,
        )


def _validate_json(target: Target, content: str) -> None:
    """Refuse broken JSON (and broken phrase files) before touching the disk."""
    try:
        data = json.loads(content)
    except ValueError as exc:
        raise FileAccessError(t("selfmod.files.bad_json", error=exc)) from exc
    if target.path.name == "fallback_phrases.json":
        try:
            validate_phrases(data)
        except PhrasesError as exc:
            raise FileAccessError(t("selfmod.files.bad_phrases", error=exc)) from exc


def _check_size(content: str) -> None:
    if len(content.encode("utf-8")) > MAX_BYTES:
        raise FileAccessError(t("selfmod.files.too_big", limit=MAX_BYTES // 1000))
