"""Files the bot may read and change by itself, and nothing else.

Allowed roots:
  * ``data/...``  -> <repo>/data (soul.md, prompts): read, overwrite, append;
                     every change becomes a git commit (UVA-Self-Edit).
  * ``brain/...`` -> the second brain: read, create new notes, append;
                     existing notes are never overwritten (and not in git).
Everything else (src/, security.py, .env, docker-compose.yml...) is refused.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from uva.selfmod.git import SELF_EDIT, GitRepo

ALLOWED_SUFFIXES = {".md", ".txt"}
MAX_BYTES = 50_000


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
        if rel.suffix.lower() not in ALLOWED_SUFFIXES:
            raise FileAccessError(f"só arquivos .md ou .txt (pedido: {raw})")

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
        target.path.parent.mkdir(parents=True, exist_ok=True)
        target.path.write_text(content, encoding="utf-8")
        return Change(target, await self._commit(target, "write", reason))

    async def append(self, raw: str, content: str, reason: str) -> Change:
        target = self.resolve(raw)
        existing = target.path.read_text(encoding="utf-8") if target.path.exists() else ""
        _check_size(existing + content)
        target.path.parent.mkdir(parents=True, exist_ok=True)
        with target.path.open("a", encoding="utf-8") as fh:
            fh.write(("\n" if existing and not existing.endswith("\n") else "") + content)
        return Change(target, await self._commit(target, "append", reason))

    async def _commit(self, target: Target, op: str, reason: str) -> str | None:
        if target.repo_relative is None or self.git is None:
            return None
        return await self.git.commit_paths(
            [target.repo_relative],
            subject=f"Self-edit: {op} {target.display}",
            body=reason or "Alteração feita pela UVA.",
            trailer=SELF_EDIT,
        )


def _check_size(content: str) -> None:
    if len(content.encode("utf-8")) > MAX_BYTES:
        raise FileAccessError(f"arquivo grande demais (limite {MAX_BYTES // 1000} KB)")
