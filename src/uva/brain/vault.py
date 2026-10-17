"""Markdown vault ("second brain"), Obsidian-compatible, organized with PARA.

Notes are plain .md files with YAML front matter and [[wiki links]]. File
names are the Portuguese title in snake_case without accents
(marmitas_frias_e_requentadas.md); the title itself, with accents, is in the
front matter and the H1, and links are [[file_name|Title]] (see naming.py).
The vault only ever creates and appends: nothing is deleted or overwritten.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, tzinfo
from pathlib import Path

import yaml

from uva.brain.naming import file_stem, link
from uva.clock import utcnow
from uva.text import normalize

FOLDERS: dict[str, str] = {
    "inbox": "00-inbox",
    "projetos": "01-projetos",
    "areas": "02-areas",
    "recursos": "03-recursos",
    "arquivo": "04-arquivo",
    "anexos": "05-anexos",
    "diario": "diario",
}
README_NAME = "LEIA-ME.md"
ARCHIVE_PREFIX = FOLDERS["arquivo"] + "/"
SOURCE = "uva-bot"
MAX_TITLE = 80
MAX_SUBFOLDER_DEPTH = 3

# Characters Obsidian / Windows don't accept in file names or link targets.
_BAD_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|#^\[\]\n\r\t]+')


class NoteNotFoundError(LookupError):
    pass


class PlaceholderError(ValueError):
    """The text has a stand-in like "<texto anterior mantido aqui>" instead of content."""


_PLACEHOLDER = re.compile(
    r"<[^<>\n]{0,80}\b(anterior|restante|mantid[oa]|conte[uú]do (original|existente)|resto)\b[^<>\n]{0,80}>"
    r"|\[(\.\.\.|…)?\s*(restante|resto|conte[uú]do anterior)[^\]]{0,60}\]",
    re.IGNORECASE,
)


def check_no_placeholders(text: str) -> None:
    if match := _PLACEHOLDER.search(text):
        raise PlaceholderError(
            f"the text has a placeholder instead of content ({match.group(0)!r}); "
            "read the note and send the complete content"
        )


@dataclass(slots=True)
class Note:
    path: str  # relative to the vault root, with forward slashes
    title: str
    body: str
    created: str = ""
    tags: list[str] = field(default_factory=list)

    @property
    def link(self) -> str:
        """[[file_name|Title]] for this note."""
        return link(Path(self.path).stem, self.title)


@dataclass(frozen=True, slots=True)
class Entry:
    """A file or folder, for read-only inspection (/brain)."""

    path: str  # relative, forward slashes
    is_dir: bool
    size: int = 0  # bytes (files) or number of .md notes inside (folders)
    modified: datetime | None = None


@dataclass(frozen=True, slots=True)
class RecentNote:
    path: str
    title: str
    changed: datetime


@dataclass(frozen=True, slots=True)
class SearchHit:
    path: str
    title: str
    snippet: str


class Vault:
    def __init__(self, root: Path, tz: tzinfo) -> None:
        self.root = root
        self.tz = tz

    # --- structure -------------------------------------------------------

    def ensure_structure(self) -> bool:
        """Create the PARA folders and LEIA-ME.md if missing. True if anything was created."""
        created = False
        for folder in FOLDERS.values():
            path = self.root / folder
            if not path.exists():
                path.mkdir(parents=True)
                created = True
        readme = self.root / README_NAME
        if not readme.exists():
            readme.write_text(README_TEXT, encoding="utf-8")
            created = True
        return created

    # --- writing ---------------------------------------------------------

    def create_note(
        self,
        title: str,
        body: str,
        *,
        folder: str = "inbox",
        subfolder: str = "",
        tags: list[str] | tuple[str, ...] = (),
        links: list[str] | tuple[str, ...] = (),
        now: datetime | None = None,
    ) -> Note:
        check_no_placeholders(body)
        title = clean_title(title)
        directory = self.folder_path(folder, subfolder)
        directory.mkdir(parents=True, exist_ok=True)
        path = _unique_path(directory, file_stem(title))

        created = self._timestamp(now)
        text = render_note(title, body, created=created, tags=list(tags), links=list(links))
        path.write_text(text, encoding="utf-8")
        return Note(self._relative(path), title, body, created, list(tags))

    def folder_path(self, folder: str, subfolder: str = "") -> Path:
        """A PARA folder, optionally with subfolders ("Saúde/Exercícios").

        Hidden names, "..", absolute paths and more than MAX_SUBFOLDER_DEPTH
        levels are refused; names are cleaned like note titles.
        """
        if folder not in FOLDERS:
            raise ValueError(f"unknown folder: {folder}")
        raw_parts = [p.strip() for p in re.split(r"[\\/]", subfolder) if p.strip()]
        if any(p.startswith(".") for p in raw_parts):
            raise ValueError("hidden folders or '..' are not allowed")
        if len(raw_parts) > MAX_SUBFOLDER_DEPTH:
            raise ValueError(f"at most {MAX_SUBFOLDER_DEPTH} levels of subfolders")
        path = self.root / FOLDERS[folder]
        for part in raw_parts:
            path = path / clean_title(part)
        if not path.resolve().is_relative_to(self.root.resolve()):
            raise ValueError("path outside the second brain")
        return path

    def create_folder(self, folder: str, name: str, now: datetime | None = None) -> tuple[str, str | None]:
        """Create a folder with an empty placeholder note (so Obsidian shows it).

        Returns (folder path, placeholder note path or None if the folder
        already had notes). Nothing existing is touched.
        """
        directory = self.folder_path(folder, name)
        directory.mkdir(parents=True, exist_ok=True)
        if any(directory.glob("*.md")):
            return self._relative(directory), None
        # A truly empty marker file (like a Python __init__.py), named after the
        # folder so it reads well in Obsidian.
        placeholder = directory / f"{directory.name}.md"
        placeholder.write_bytes(b"")
        return self._relative(directory), self._relative(placeholder)

    def update_note(
        self,
        ref: str,
        *,
        title: str | None = None,
        body: str | None = None,
        tags: list[str] | None = None,
        now: datetime | None = None,
    ) -> tuple[Note, str]:
        """Rewrite and/or rename a note. The previous version is copied to
        04-arquivo/Versões first, so nothing is ever lost. Returns (note, backup)."""
        path = self._resolve(ref)
        old_text = path.read_text(encoding="utf-8")
        meta, old_body = parse_note(old_text)
        new_body = old_body if body is None else body
        check_no_placeholders(new_body)

        stamp = (now or utcnow()).astimezone(self.tz).strftime("%Y-%m-%d %H%M%S")
        versions = self.root / FOLDERS["arquivo"] / "Versões"
        versions.mkdir(parents=True, exist_ok=True)
        backup = _unique_path(versions, file_stem(f"{path.stem} {stamp}"))
        backup.write_text(old_text, encoding="utf-8")

        new_title = clean_title(title) if title else str(meta.get("title") or path.stem)
        target = path
        if file_stem(new_title) != path.stem:
            target = _unique_path(path.parent, file_stem(new_title))
        if body is None:
            # Keep the text, but fix the H1 that repeats the old title.
            old_heading = f"# {meta.get('title') or path.stem}"
            if new_body.lstrip().startswith(old_heading + "\n") or new_body.strip() == old_heading:
                new_body = new_body.lstrip()[len(old_heading):].lstrip("\n")
        created = str(meta.get("created") or self._timestamp(now))
        new_tags = tags if tags is not None else list(meta.get("tags") or [])
        target.write_text(render_note(new_title, new_body, created=created, tags=new_tags, links=[]), encoding="utf-8")
        if target != path:
            path.unlink()  # the old content lives on in the backup copy
        return self.read(self._relative(target)), self._relative(backup)

    def append(self, ref: str, text: str, now: datetime | None = None) -> Note:
        check_no_placeholders(text)
        path = self._resolve(ref)
        stamp = (now or utcnow()).astimezone(self.tz).strftime("%d/%m/%Y %H:%M")
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"\n\n### {stamp}\n\n{text.strip()}\n")
        return self.read(self._relative(path))

    def append_daily(self, text: str, now: datetime | None = None) -> Note:
        """Add a line to today's journal note (diario/AAAA-MM-DD.md)."""
        local = (now or utcnow()).astimezone(self.tz)
        directory = self.root / FOLDERS["diario"]
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{local:%Y-%m-%d}.md"
        if not path.exists():
            header = render_note(
                f"{local:%Y-%m-%d}", "", created=self._timestamp(now), tags=["diario"], links=[]
            )
            path.write_text(header.rstrip() + "\n", encoding="utf-8")
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"\n- {local:%H:%M} {text.strip()}")
        return self.read(self._relative(path))

    # --- reading ---------------------------------------------------------

    def read(self, ref: str) -> Note:
        path = self._resolve(ref)
        meta, body = parse_note(path.read_text(encoding="utf-8"))
        return Note(
            path=self._relative(path),
            title=str(meta.get("title") or path.stem),
            body=body,
            created=str(meta.get("created", "")),
            tags=list(meta.get("tags") or []),
        )

    def search(self, term: str, limit: int = 10) -> list[SearchHit]:
        """Accent/case-insensitive match on title and content; title hits first."""
        needle = normalize(term)
        if not needle:
            return []
        title_hits: list[SearchHit] = []
        body_hits: list[SearchHit] = []
        for path in sorted(self.root.rglob("*.md")):
            content = path.read_text(encoding="utf-8", errors="replace")
            meta, body = parse_note(content)
            title = str(meta.get("title") or path.stem)
            hit = SearchHit(self._relative(path), title, _snippet(body, needle))
            if needle in normalize(title):
                title_hits.append(hit)
            elif needle in normalize(body):
                body_hits.append(hit)
        return (title_hits + body_hits)[:limit]

    def save_attachment(self, data: bytes, extension: str, title: str) -> str:
        """Store a binary file (image) in 05-anexos without overwriting; returns its path."""
        directory = self.root / FOLDERS["anexos"]
        directory.mkdir(parents=True, exist_ok=True)
        stem = file_stem(title)[:60]
        path = directory / f"{stem}.{extension}"
        counter = 2
        while path.exists():
            path = directory / f"{stem}_{counter}.{extension}"
            counter += 1
        path.write_bytes(data)
        return self._relative(path)

    def iter_notes(self) -> Iterator[tuple[str, str, str]]:
        """(path, title, body) of every visible note (LEIA-ME excluded)."""
        for path in sorted(self.root.rglob("*.md")):
            if path.name == README_NAME or not self._visible(path):
                continue
            meta, body = parse_note(path.read_text(encoding="utf-8", errors="replace"))
            yield self._relative(path), str(meta.get("title") or path.stem), body

    def recent(self, limit: int = 10) -> list[RecentNote]:
        """Most recently modified notes (LEIA-ME excluded)."""
        paths = [p for p in self.root.rglob("*.md") if p.name != README_NAME]
        paths.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        notes = []
        for path in paths[:limit]:
            meta, _ = parse_note(path.read_text(encoding="utf-8", errors="replace"))
            changed = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
            notes.append(RecentNote(self._relative(path), str(meta.get("title") or path.stem), changed))
        return notes

    # --- helpers ---------------------------------------------------------

    # --- read-only inspection (/brain) -------------------------------------------

    def _visible(self, path: Path) -> bool:
        """Inside the vault and not hidden (.obsidian, .trash...)."""
        rel = path.resolve().relative_to(self.root.resolve())
        return not any(part.startswith(".") for part in rel.parts)

    def find_folder(self, ref: str) -> Path | None:
        """A folder by relative path ("02-areas/Saúde") or PARA name ("areas")."""
        ref = ref.strip().strip("/")
        if not ref:
            return self.root
        candidates = [self.root / FOLDERS[ref]] if ref in FOLDERS else []
        candidates.append(self.root / ref)
        root = self.root.resolve()
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved.is_relative_to(root) and resolved.is_dir() and self._visible(resolved):
                return resolved
        return None

    def list_folder(self, folder: Path) -> list[Entry]:
        """Visible subfolders (with note counts) and .md files, folders first."""
        entries = []
        for child in sorted(folder.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            if child.name.startswith("."):
                continue
            stat = child.stat()
            modified = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
            if child.is_dir():
                notes = sum(1 for p in child.rglob("*.md") if self._visible(p))
                entries.append(Entry(self._relative(child), True, notes, modified))
            elif child.suffix == ".md":
                entries.append(Entry(self._relative(child), False, stat.st_size, modified))
        return entries

    def inspect(self, ref: str) -> tuple[Entry, str]:
        """A note's metadata and raw text (front matter included)."""
        path = self._resolve(ref)
        if path.suffix != ".md" or not self._visible(path):
            raise NoteNotFoundError(ref)
        stat = path.stat()
        entry = Entry(self._relative(path), False, stat.st_size, datetime.fromtimestamp(stat.st_mtime, tz=UTC))
        return entry, path.read_text(encoding="utf-8", errors="replace")

    def _resolve(self, ref: str) -> Path:
        """Find a note by relative path or by title. Never leaves the vault."""
        ref = ref.strip().removeprefix("[[").removesuffix("]]")
        candidate = (self.root / ref).resolve()
        if not candidate.suffix:
            candidate = candidate.with_suffix(".md")
        root = self.root.resolve()
        if candidate.is_relative_to(root) and candidate.is_file():
            return candidate

        wanted = normalize(Path(ref).stem)
        notes = [p for p in sorted(self.root.rglob("*.md")) if self._visible(p)]
        stem = file_stem(Path(ref).stem)
        for path in notes:
            if normalize(path.stem) == wanted or path.stem == stem:
                return path
        for path in notes:  # by the title in the front matter ("Marmitas frias e requentadas")
            meta, _ = parse_note(path.read_text(encoding="utf-8", errors="replace"))
            if meta.get("title") and normalize(str(meta["title"])) == wanted:
                return path
        # Partial title ("marmitas"): accept it only when it is unambiguous.
        partial = [p for p in notes if wanted and wanted in normalize(p.stem)]
        if len(partial) == 1:
            return partial[0]
        raise NoteNotFoundError(ref)

    def _relative(self, path: Path) -> str:
        return path.resolve().relative_to(self.root.resolve()).as_posix()

    def _timestamp(self, now: datetime | None) -> str:
        return (now or utcnow()).astimezone(self.tz).isoformat(timespec="seconds")


def clean_title(title: str) -> str:
    title = " ".join(_BAD_FILENAME_CHARS.sub(" ", title).split()).strip(" .")
    return title[:MAX_TITLE].rstrip() or "Sem título"


def title_from_text(text: str, max_words: int = 8) -> str:
    first_line = text.strip().splitlines()[0] if text.strip() else ""
    words = first_line.split()
    title = " ".join(words[:max_words])
    return clean_title(title + ("…" if len(words) > max_words else ""))


def render_note(title: str, body: str, *, created: str, tags: list[str], links: list[str]) -> str:
    meta = {"title": title, "created": created, "tags": tags, "source": SOURCE}
    front = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).strip()
    parts = [f"---\n{front}\n---"]
    if not body.lstrip().startswith("# "):  # the body may bring its own title
        parts.append(f"# {title}")
    if body.strip():
        parts.append(body.strip())
    if links:
        parts.append("Relacionado: " + " ".join(link(file_stem(name), clean_title(name)) for name in links))
    return "\n\n".join(parts) + "\n"


def parse_note(text: str) -> tuple[dict, str]:
    """Split YAML front matter from the body (tolerates notes without it)."""
    if text.startswith("---"):
        parts = text.split("\n---", 1)
        if len(parts) == 2:
            try:
                meta = yaml.safe_load(parts[0].removeprefix("---")) or {}
            except yaml.YAMLError:
                meta = {}
            if isinstance(meta, dict):
                return meta, parts[1].lstrip("-").lstrip("\n")
    return {}, text


def _unique_path(directory: Path, stem: str) -> Path:
    path = directory / f"{stem}.md"
    counter = 2
    while path.exists():
        path = directory / f"{stem}_{counter}.md"
        counter += 1
    return path


def _snippet(body: str, needle: str, width: int = 120) -> str:
    flat = " ".join(body.split())
    index = normalize(flat).find(needle)
    if index < 0:
        return flat[:width]
    start = max(0, index - width // 3)
    return ("…" if start else "") + flat[start : start + width] + ("…" if start + width < len(flat) else "")


README_TEXT = """---
title: LEIA-ME
source: uva-bot
---

# Second brain da UVA

Este cofre (vault) é escrito pela UVA e pode ser aberto no Obsidian.

## Organização (método PARA)

- `00-inbox/` — entrada: notas rápidas (/nota) ainda não organizadas
- `01-projetos/` — projetos com objetivo e prazo (inclui os resultados dos objetivos da UVA)
- `02-areas/` — áreas de responsabilidade contínuas (saúde, finanças, casa...)
- `03-recursos/` — material de referência e pesquisas feitas pela UVA
- `04-arquivo/` — o que não está mais ativo
- `05-anexos/` — imagens e outros arquivos que a UVA guardou (embutidos nas notas com `![[...]]`)
- `diario/` — uma nota por dia (AAAA-MM-DD.md) com o que aconteceu

## Notas (Zettelkasten)

- Uma ideia por nota, com título claro (o nome do arquivo é o título).
- Cabeçalho YAML com `title`, `created`, `tags` e `source: uva-bot`.
- Ligações entre notas com `[[Título da nota]]`.

## Regras da UVA

- A UVA só cria notas e acrescenta texto; ela nunca apaga nem sobrescreve nada.
- Para mover ou apagar notas, faça você mesmo (no Obsidian ou no explorador de arquivos).
"""
