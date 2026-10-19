"""Directive-style preferences in data/profile.md.

The profile's ``## Preferences`` section holds one directive per line:

    - Sempre responder em tópicos curtos <!-- observed: 2026-10-19 | status: active -->

A new preference that replaces an old one does not get appended next to it:
the old line is marked ``superseded`` (its date kept) and the new one goes
right below it (:func:`supersede`). ``/perfil consolidar`` drops superseded
lines older than :data:`SUPERSEDED_KEEP_DAYS` (:func:`drop_superseded`) and
refuses a rewrite that loses more than PROFILE_MAX_LOSS of the active
directives and facts (:func:`loss`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

from rapidfuzz import fuzz

from uva.text import normalize

SECTION = "## Preferences"
ACTIVE, SUPERSEDED = "active", "superseded"
SUPERSEDED_KEEP_DAYS = 60
# A fact or directive counts as kept when it is this similar to part of the new text.
KEPT_SCORE = 70
_LINE = re.compile(
    r"^\s*-\s*(?P<text>.*?)\s*<!--\s*observed:\s*(?P<date>\d{4}-\d{2}-\d{2})\s*\|\s*status:\s*(?P<status>[a-z]+)\s*-->\s*$"
)
_HEADING = re.compile(r"^#{1,6}\s")


@dataclass(frozen=True, slots=True)
class Directive:
    text: str
    observed: date
    status: str
    line: int  # index in the profile's lines


def render(text: str, observed: date, status: str = ACTIVE) -> str:
    return f"- {text.strip()} <!-- observed: {observed.isoformat()} | status: {status} -->"


def _section(lines: list[str]) -> tuple[int, int] | None:
    """(first line after the heading, end) of the Preferences section, or None."""
    start = next((i for i, line in enumerate(lines) if line.strip().lower() == SECTION.lower()), None)
    if start is None:
        return None
    end = next((i for i in range(start + 1, len(lines)) if _HEADING.match(lines[i])), len(lines))
    return start + 1, end


def parse(profile: str) -> list[Directive]:
    lines = profile.splitlines()
    bounds = _section(lines)
    if bounds is None:
        return []
    out = []
    for index in range(*bounds):
        if m := _LINE.match(lines[index]):
            out.append(Directive(m["text"], date.fromisoformat(m["date"]), m["status"], index))
    return out


def _find(directives: list[Directive], hint: str) -> Directive | None:
    """The active directive ``hint`` names (its text or a part of it)."""
    hint_norm = normalize(hint)
    if not hint_norm:
        return None
    active = [d for d in directives if d.status == ACTIVE]
    for directive in active:
        if hint_norm in normalize(directive.text):
            return directive
    scored = [(fuzz.token_set_ratio(hint_norm, normalize(d.text)), d) for d in active]
    best = max(scored, default=(0, None), key=lambda p: p[0])
    return best[1] if best[0] >= 80 else None


def supersede(profile: str, new: str, old_hint: str = "", today: date | None = None) -> tuple[str, Directive | None]:
    """Add the directive ``new``; when ``old_hint`` names an active one, mark it
    superseded and put ``new`` right below it. Returns (profile, superseded)."""
    today = today or date.today()
    lines = profile.splitlines()
    bounds = _section(lines)
    if bounds is None:
        body = profile.rstrip("\n")
        return (f"{body}\n\n" if body else "") + f"{SECTION}\n{render(new, today)}\n", None
    old = _find(parse(profile), old_hint) if old_hint else None
    if old is not None:
        lines[old.line] = render(old.text, old.observed, SUPERSEDED)
        lines.insert(old.line + 1, render(new, today))
    else:
        start, end = bounds
        while end > start and not lines[end - 1].strip():
            end -= 1
        lines.insert(end, render(new, today))
    return "\n".join(lines) + "\n", old


def drop_superseded(profile: str, today: date | None = None, days: int = SUPERSEDED_KEEP_DAYS) -> tuple[str, int]:
    """Superseded directives observed more than ``days`` ago go away."""
    today = today or date.today()
    cutoff = today - timedelta(days=days)
    gone = {d.line for d in parse(profile) if d.status == SUPERSEDED and d.observed < cutoff}
    if not gone:
        return profile, 0
    kept = [line for i, line in enumerate(profile.splitlines()) if i not in gone]
    return "\n".join(kept) + "\n", len(gone)


def split_section(profile: str) -> tuple[str, str]:
    """(profile without the Preferences section, the section itself)."""
    lines = profile.splitlines()
    bounds = _section(lines)
    if bounds is None:
        return profile, ""
    start, end = bounds
    section = "\n".join(lines[start - 1:end]).strip()
    rest = "\n".join(lines[: start - 1] + lines[end:]).strip()
    return rest + "\n", section


def items(profile: str) -> list[str]:
    """Active directives and plain bullet facts (superseded lines left out)."""
    out = []
    for line in profile.splitlines():
        stripped = line.strip()
        if m := _LINE.match(line):
            if m["status"] == ACTIVE:
                out.append(m["text"])
        elif stripped.startswith(("- ", "* ")) and len(stripped) > 3:
            out.append(stripped[2:])
    return out


def loss(before: str, after: str) -> float:
    """Share of the active directives and facts of ``before`` missing in ``after``."""
    wanted = items(before)
    if not wanted:
        return 0.0
    target = normalize(after)
    missing = sum(1 for item in wanted if fuzz.partial_ratio(normalize(item), target) < KEPT_SCORE)
    return missing / len(wanted)
