"""Files the bot may read and change by itself, and nothing else.

Allowed roots:
  * ``data/...``  -> <repo>/data (soul.md, fallback_phrases.json, prompts): read,
                     overwrite, append (.md/.txt); every change becomes a git
                     commit (UVA-Self-Edit). JSON files are validated before
                     they are written and can't be appended to.
  * ``brain/...`` -> the second brain: read, create new notes, append;
                     existing notes are never overwritten (and not in git).
Everything else (src/, security.py, .env, docker-compose.yml...) is refused.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from uva.brain.vault import PlaceholderError, check_no_placeholders
from uva.phrases import PhrasesError
from uva.phrases import validate as validate_phrases
from uva.selfmod.git import SELF_EDIT, GitRepo

ALLOWED_SUFFIXES = {".md", ".txt"}
DATA_ONLY_SUFFIXES = {".json"}
MAX_BYTES = 50_000
# Personal files kept out of git (see .gitignore): edits keep a copy in data/.versions/.
UNTRACKED = {"data/profile.md"}


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
    def __init__(self, repo_dir: Path, brain_dir: Path | None, git: GitRepo | None) -> None:
        self.roots: dict[str, Path] = {"data": repo_dir / "data"}
        if brain_dir is not None:
            self.roots["brain"] = brain_dir
        self.repo_dir = repo_dir
        self.git = git

    def resolve(self, raw: str) -> Target:
        rel = PurePosixPath(raw.strip().replace("\\", "/"))
        parts = rel.parts
        if rel.is_absolute() or len(parts) < 2 or any(p in ("", ".", "..") for p in parts):
            raise FileAccessError(f"caminho não permitido: {raw}")
        if any(p.startswith(".") for p in parts):
            raise FileAccessError(f"arquivos ocultos não são permitidos: {raw}")
        root_name = parts[0]
        if root_name not in self.roots:
            allowed = ", ".join(f"{r}/" for r in self.roots)
            raise FileAccessError(f"só posso mexer em {allowed} (pedido: {raw})")
        suffix = rel.suffix.lower()
        if suffix not in ALLOWED_SUFFIXES and not (root_name == "data" and suffix in DATA_ONLY_SUFFIXES):
            raise FileAccessError(f"só arquivos .md ou .txt (ou .json em data/) (pedido: {raw})")

        root = self.roots[root_name].resolve()
        path = (root / PurePosixPath(*parts[1:])).resolve()
        if not path.is_relative_to(root):
            raise FileAccessError(f"caminho não permitido: {raw}")
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
        if target.root == "brain" and target.path.exists():
            raise FileAccessError("notas do second brain não são sobrescritas; use file_append")
        if target.path.suffix == ".json":
            _validate_json(target, content)
        try:
            check_no_placeholders(content)
        except PlaceholderError as exc:
            raise FileAccessError(str(exc)) from exc
        target.path.parent.mkdir(parents=True, exist_ok=True)
        self._backup_untracked(target)
        target.path.write_text(content, encoding="utf-8")
        return Change(target, await self._commit(target, "write", reason))

    async def append(self, raw: str, content: str, reason: str) -> Change:
        target = self.resolve(raw)
        if target.path.suffix == ".json":
            raise FileAccessError("arquivos JSON não aceitam append; use file_write com o conteúdo completo")
        existing = target.path.read_text(encoding="utf-8") if target.path.exists() else ""
        _check_size(existing + content)
        target.path.parent.mkdir(parents=True, exist_ok=True)
        self._backup_untracked(target)
        with target.path.open("a", encoding="utf-8") as fh:
            fh.write(("\n" if existing and not existing.endswith("\n") else "") + content)
        return Change(target, await self._commit(target, "append", reason))

    def _backup_untracked(self, target: Target) -> None:
        """Files outside git (data/profile.md is gitignored): keep the old version."""
        if target.repo_relative not in UNTRACKED or not target.path.exists():
            return
        from datetime import datetime

        versions = target.path.parent / ".versions"
        versions.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        (versions / f"{target.path.stem}-{stamp}{target.path.suffix}").write_text(
            target.path.read_text(encoding="utf-8"), encoding="utf-8"
        )

    async def _commit(self, target: Target, op: str, reason: str) -> str | None:
        if target.repo_relative is None or self.git is None or target.repo_relative in UNTRACKED:
            return None
        return await self.git.commit_paths(
            [target.repo_relative],
            subject=f"Self-edit: {op} {target.display}",
            body=reason or "Alteração feita pela UVA.",
            trailer=SELF_EDIT,
        )


def _validate_json(target: Target, content: str) -> None:
    """Refuse broken JSON (and broken phrase files) before touching the disk."""
    try:
        data = json.loads(content)
    except ValueError as exc:
        raise FileAccessError(f"JSON inválido: {exc}") from exc
    if target.path.name == "fallback_phrases.json":
        try:
            validate_phrases(data)
        except PhrasesError as exc:
            raise FileAccessError(f"arquivo de frases inválido: {exc}") from exc


def _check_size(content: str) -> None:
    if len(content.encode("utf-8")) > MAX_BYTES:
        raise FileAccessError(f"arquivo grande demais (limite {MAX_BYTES // 1000} KB)")
