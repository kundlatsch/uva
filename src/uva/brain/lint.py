"""Weekly brain check (beat, Sunday at MEMORY_MAINTENANCE_HOUR): it only reports.

Finds broken [[links]], notes in 03-recursos with no links in or out,
research notes older than 180 days and likely duplicates (title similarity of
90+ in the same folder), writes them to ``00-inbox/Revisão do cérebro
<date>.md`` and sends one line with the counts. Nothing is fixed on its own.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import PurePosixPath

from rapidfuzz import fuzz

from uva.brain.naming import file_stem
from uva.brain.vault import ARCHIVE_PREFIX, FOLDERS, README_NAME, parse_note
from uva.clock import format_local, utcnow
from uva.i18n import t
from uva.text import normalize

LINK = re.compile(r"!?\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
OLD_RESEARCH_DAYS = 180
DUPLICATE_SCORE = 90
REPORT_TITLE = "Revisão do cérebro"
LIST_LIMIT = 30


@dataclass
class Findings:
    broken: list[tuple[str, str]] = field(default_factory=list)  # (note, target)
    orphans: list[str] = field(default_factory=list)
    old_research: list[str] = field(default_factory=list)
    duplicates: list[tuple[str, str]] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.broken) + len(self.orphans) + len(self.old_research) + len(self.duplicates)


def _created(meta: dict, path, tz) -> datetime | None:
    value = meta.get("created")
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=tz)
    try:
        parsed = datetime.fromisoformat(str(value))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=tz)
    except ValueError:
        return datetime.fromtimestamp(path.stat().st_mtime, tz)


def scan(vault, now: datetime | None = None) -> Findings:
    """Read the whole vault (blocking: run it in a thread)."""
    now = now or utcnow()
    root = vault.root
    notes = {}
    for path in sorted(root.rglob("*.md")):
        rel = path.relative_to(root).as_posix()
        if path.name == README_NAME or rel.startswith(ARCHIVE_PREFIX) or not vault._visible(path):
            continue
        meta, body = parse_note(path.read_text(encoding="utf-8", errors="replace"))
        notes[rel] = (path, meta, body)
    files = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
    stems = {PurePosixPath(rel).stem.lower() for rel in files}
    names = {PurePosixPath(rel).name.lower() for rel in files} | {rel.lower() for rel in files}

    found = Findings()
    incoming: dict[str, int] = {rel: 0 for rel in notes}
    by_stem = {PurePosixPath(rel).stem.lower(): rel for rel in notes}
    outgoing: dict[str, int] = {}
    for rel, (path, meta, body) in notes.items():
        targets = [m.strip() for m in LINK.findall(body)]
        outgoing[rel] = len(targets)
        for target in targets:
            key = target.lower()
            stem = PurePosixPath(key).stem if "." in PurePosixPath(key).name else PurePosixPath(key).name
            if key in names or stem in stems or file_stem(target) in stems:
                if (hit := by_stem.get(stem) or by_stem.get(file_stem(target))) is not None:
                    incoming[hit] += 1
                continue
            found.broken.append((rel, target))
    recursos = FOLDERS["recursos"] + "/"
    for rel in notes:
        if rel.startswith(recursos) and not outgoing.get(rel) and not incoming.get(rel):
            found.orphans.append(rel)
    cutoff = now - timedelta(days=OLD_RESEARCH_DAYS)
    for rel, (path, meta, body) in notes.items():
        tags = meta.get("tags") or []
        is_research = "pesquisa" in tags or str(meta.get("title", "")).startswith("Pesquisa - ")
        if is_research and (created := _created(meta, path, vault.tz)) is not None and created < cutoff:
            found.old_research.append(rel)
    by_folder: dict[str, list[tuple[str, str]]] = {}
    for rel, (path, meta, body) in notes.items():
        if rel.startswith(FOLDERS["diario"] + "/") or "revisao" in (meta.get("tags") or []):
            continue  # dated notes look alike on purpose
        title = normalize(str(meta.get("title") or path.stem))
        by_folder.setdefault(str(PurePosixPath(rel).parent), []).append((rel, title))
    for items in by_folder.values():
        for i, (a, title_a) in enumerate(items):
            for b, title_b in items[i + 1:]:
                if title_a and title_b and fuzz.ratio(title_a, title_b) >= DUPLICATE_SCORE:
                    found.duplicates.append((a, b))
    return found


def report_body(found: Findings, now: datetime, tz) -> str:
    """The report note (brain content: Portuguese whatever the bot language)."""
    def section(key: str, lines: list[str]) -> list[str]:
        if not lines:
            return []
        extra = [t("brain.lint.more", lang="pt", n=len(lines) - LIST_LIMIT)] if len(lines) > LIST_LIMIT else []
        return ["", f"## {t(f'brain.lint.section.{key}', lang='pt', n=len(lines))}", "", *lines[:LIST_LIMIT], *extra]

    out = [t("brain.lint.intro", lang="pt", when=format_local(now, tz, "%d/%m/%Y %H:%M"))]
    out += section("broken", [f"- [[{PurePosixPath(n).stem}]] → `[[{target}]]`" for n, target in found.broken])
    out += section("orphans", [f"- [[{PurePosixPath(n).stem}]]" for n in found.orphans])
    out += section("old_research", [f"- [[{PurePosixPath(n).stem}]]" for n in found.old_research])
    out += section("duplicates", [f"- [[{PurePosixPath(a).stem}]] ≈ [[{PurePosixPath(b).stem}]]"
                                  for a, b in found.duplicates])
    if not found.total:
        out += ["", t("brain.lint.clean", lang="pt")]
    return "\n".join(out)


async def brain_lint(services, now: datetime | None = None) -> tuple[Findings | None, str | None]:
    """(findings, the line for the user) — or (None, None) without a vault."""
    import asyncio

    vault = services.vault
    if vault is None:
        return None, None
    now = now or utcnow()
    found = await asyncio.to_thread(scan, vault, now)
    day = format_local(now, services.tz, "%Y-%m-%d")
    note = vault.create_note(f"{REPORT_TITLE} {day}", report_body(found, now, services.tz), folder="inbox",
                             tags=["revisao"], check=False)
    line = t("brain.lint.notice", broken=len(found.broken), orphans=len(found.orphans),
             old=len(found.old_research), duplicates=len(found.duplicates), note=note.path)
    return found, line
