"""Skills: how to do a class of task the way the user wants (procedural memory).

A skill is a folder ``data/skills/<name>/`` with a ``SKILL.md``:

    ---
    name: reading-notes
    description: Records what the user says about a book he read (<= 120 chars)
    triggers: ["terminei de ler", "li o livro"]   # optional example phrases
    version: 1
    ---
    ## When to Use
    ## Procedure
    ## Pitfalls
    ## Verification

plus optional ``references/*.md``. Skills are in English (only the LLM reads
them). The system prompt carries only the index (name + description); the
body is loaded on demand (``skill_read``, ``/skill``, a trigger match, or a
routine/goal that names the skill). Changes go through FileAccess, so each
one is a commit with ``UVA-Self-Edit`` that ``/reverter`` can undo.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml
from rapidfuzz import fuzz

from uva.clock import utcnow
from uva.logging import get_logger
from uva.storage.sqlite import Database
from uva.text import normalize

log = get_logger(__name__)

SKILL_FILE = "SKILL.md"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")
MAX_DESCRIPTION = 120
MAX_BODY = 12000
REQUIRED_SECTIONS = ("## When to Use", "## Procedure")
SECTIONS = ("## When to Use", "## Procedure", "## Pitfalls", "## Verification")
# A trigger phrase must appear (fuzzily) in the message to attach its skill.
TRIGGER_THRESHOLD = 90
MIN_TRIGGER_CHARS = 8


class SkillError(ValueError):
    """Invalid skill (name, front matter, body) or a refused change."""


@dataclass(frozen=True, slots=True)
class Skill:
    name: str
    description: str
    body: str
    triggers: tuple[str, ...] = ()
    version: int = 1
    references: tuple[str, ...] = ()  # file names under references/

    @property
    def path(self) -> str:
        return f"data/skills/{self.name}/{SKILL_FILE}"


@dataclass(frozen=True, slots=True)
class Usage:
    name: str
    uses: int
    last_used_at: datetime | None


def check_name(name: str) -> str:
    name = name.strip().lower()
    if not NAME_RE.match(name):
        raise SkillError(f"invalid skill name {name!r}: use 2-40 chars, lowercase letters, digits and '-'")
    return name


def render(skill: Skill) -> str:
    meta: dict = {"name": skill.name, "description": skill.description}
    if skill.triggers:
        meta["triggers"] = list(skill.triggers)
    meta["version"] = skill.version
    front = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).strip()
    return f"---\n{front}\n---\n\n{skill.body.strip()}\n"


def parse(text: str, folder_name: str, references: tuple[str, ...] = ()) -> Skill:
    """Validate a SKILL.md; raises SkillError."""
    if not text.startswith("---"):
        raise SkillError("SKILL.md must start with YAML front matter (---)")
    head, sep, body = text[3:].partition("\n---")
    if not sep:
        raise SkillError("front matter is not closed (---)")
    try:
        meta = yaml.safe_load(head) or {}
    except yaml.YAMLError as exc:
        raise SkillError(f"invalid front matter: {exc}") from exc
    if not isinstance(meta, dict):
        raise SkillError("front matter must be a mapping")
    name = check_name(str(meta.get("name") or ""))
    if name != folder_name:
        raise SkillError(f"name {name!r} differs from its folder {folder_name!r}")
    description = " ".join(str(meta.get("description") or "").split())
    if not description:
        raise SkillError("description is required")
    if len(description) > MAX_DESCRIPTION:
        raise SkillError(f"description has {len(description)} chars (max {MAX_DESCRIPTION})")
    triggers = meta.get("triggers") or []
    if not isinstance(triggers, list) or not all(isinstance(x, str) for x in triggers):
        raise SkillError("triggers must be a list of phrases")
    body = body.lstrip("-").strip()
    check_body(body)
    try:
        version = int(meta.get("version") or 1)
    except (TypeError, ValueError) as exc:
        raise SkillError("version must be a number") from exc
    return Skill(name, description, body, tuple(t.strip() for t in triggers if t.strip()), version, references)


def check_body(body: str) -> None:
    missing = [s for s in REQUIRED_SECTIONS if s.lower() not in body.lower()]
    if missing:
        raise SkillError(f"the body needs the sections {', '.join(missing)} (also: {', '.join(SECTIONS[2:])})")
    if len(body) > MAX_BODY:
        raise SkillError(f"the body has {len(body)} chars (max {MAX_BODY}); move details to references/")


class SkillLibrary:
    """Loads data/skills/*/SKILL.md (re-read when a file changes)."""

    def __init__(self, root: Path, db: Database | None = None) -> None:
        self.root = root
        self.db = db
        self._cache: dict[str, tuple[tuple[float, float], Skill]] = {}
        # Skills read in this process (name -> mtime then): patching needs a fresh read.
        self._read: dict[str, float] = {}
        # Hidden from the index (plugin curator: unused for too long); still usable.
        self.idle: set[str] = set()

    # --- loading ------------------------------------------------------------

    def _file(self, name: str) -> Path:
        return self.root / name / SKILL_FILE

    def _mtime(self, name: str) -> float | None:
        try:
            return self._file(name).stat().st_mtime
        except OSError:
            return None

    def get(self, name: str) -> Skill | None:
        try:
            name = check_name(name)
        except SkillError:
            return None
        mtime = self._mtime(name)
        if mtime is None:
            self._cache.pop(name, None)
            return None
        refs_dir = self.root / name / "references"
        # A new reference file changes the folder's mtime: part of the cache key.
        key = (mtime, refs_dir.stat().st_mtime if refs_dir.is_dir() else 0.0)
        cached = self._cache.get(name)
        if cached and cached[0] == key:
            return cached[1]
        try:
            refs = tuple(sorted(p.name for p in refs_dir.glob("*.md"))) if refs_dir.is_dir() else ()
            skill = parse(self._file(name).read_text(encoding="utf-8"), name, refs)
        except (OSError, SkillError) as exc:
            log.warning("skill_invalid", skill=name, error=str(exc))
            return None
        self._cache[name] = (key, skill)
        return skill

    def all(self) -> list[Skill]:
        if not self.root.is_dir():
            return []
        skills = [self.get(p.name) for p in sorted(self.root.iterdir()) if (p / SKILL_FILE).is_file()]
        return [s for s in skills if s is not None]

    def index_text(self) -> str:
        """The block for the system prompt ('' without skills)."""
        skills = [s for s in self.all() if s.name not in self.idle]
        if not skills:
            return ""
        lines = "\n".join(f"- {s.name}: {s.description}" for s in skills)
        return (
            "## Your skills (how the user wants these tasks done)\n"
            "When a request fits one, read it first with skill_read and follow it.\n\n" + lines
        )

    def read(self, name: str, file: str = "") -> str:
        """SKILL.md (or one reference file) as text, recording the read."""
        skill = self.get(name)
        if skill is None:
            raise SkillError(f"no skill named {name!r}")
        if file:
            refs_dir = self.root / skill.name / "references"
            path = refs_dir / Path(file).name
            if path.suffix != ".md" or not path.is_file():
                found = sorted(p.name for p in refs_dir.glob("*.md")) if refs_dir.is_dir() else []
                raise SkillError(f"{name} has no reference {file!r} (has: {', '.join(found) or 'none'})")
            return path.read_text(encoding="utf-8")
        self._read[skill.name] = self._mtime(skill.name) or 0.0
        return render(skill)

    def was_read(self, name: str) -> bool:
        """True if the skill was read in this process and has not changed since."""
        seen = self._read.get(name)
        return seen is not None and seen == self._mtime(name)

    def forget_read(self, name: str) -> None:
        self._read.pop(name, None)

    # --- routing ----------------------------------------------------------------

    def match(self, text: str) -> Skill | None:
        """A skill whose name or trigger phrase is in the message, or None."""
        flat = normalize(text)
        if not flat:
            return None
        best: tuple[float, Skill] | None = None
        for skill in self.all():
            phrases = [skill.name.replace("-", " "), *skill.triggers]
            for phrase in phrases:
                target = normalize(phrase)
                if len(target) < MIN_TRIGGER_CHARS or len(target) > len(flat) + 5:
                    continue
                value = fuzz.partial_ratio(target, flat)
                if value >= TRIGGER_THRESHOLD and (best is None or value > best[0]):
                    best = (value, skill)
        return best[1] if best else None

    # --- usage ---------------------------------------------------------------------

    async def used(self, name: str) -> None:
        if self.db is None:
            return

        def job(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO skill_usage (name, uses, last_used_at) VALUES (?, 1, ?) "
                "ON CONFLICT(name) DO UPDATE SET uses = uses + 1, last_used_at = excluded.last_used_at",
                (name, utcnow().isoformat()),
            )

        try:
            await self.db.run(job)
        except Exception as exc:
            log.warning("skill_usage_failed", skill=name, error=str(exc))

    async def usage(self) -> dict[str, Usage]:
        if self.db is None:
            return {}

        def job(conn: sqlite3.Connection) -> dict[str, Usage]:
            return {
                row["name"]: Usage(
                    row["name"], row["uses"],
                    datetime.fromisoformat(row["last_used_at"]) if row["last_used_at"] else None,
                )
                for row in conn.execute("SELECT * FROM skill_usage")
            }

        return await self.db.run(job)


def attach(skill: Skill, references: str = "") -> str:
    """The block put in the message context when a skill applies to a request."""
    text = (
        f"Skill to follow for this request: {skill.name} ({skill.description}). "
        "It is how the user wants this done:\n" + skill.body.strip()
    )
    if skill.references:
        text += f"\n(References you can read with skill_read: {', '.join(skill.references)})"
    if references:
        text += "\n" + references
    return text
