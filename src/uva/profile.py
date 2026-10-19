"""Budget of the always-loaded files (profile.md, soul.md) and profile consolidation.

Both files go into every LLM call, so they have a size cap
(PROFILE_MAX_CHARS, SOUL_MAX_CHARS). Self-edits that would go over it are
refused (uva/selfmod/files.py). ``/perfil consolidar`` shrinks the profile
into a core that fits; the full previous text is kept in data/.versions/ and
in the brain note 02-areas/Gustavo/perfil_detalhado.md, which recall finds
through the search index. It never runs on its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from uva.i18n import t
from uva.llm.base import LLMError, LLMMessage
from uva.llm.budget import Scope
from uva.logging import get_logger
from uva.logging.events import EventType, emit
from uva import preferences
from uva.services import Services

log = get_logger(__name__)

DETAILED_FOLDER = "areas"
DETAILED_SUBFOLDER = "Gustavo"
DETAILED_TITLE = "Perfil detalhado"
DETAILED_PATH = "02-areas/Gustavo/perfil_detalhado.md"
# Room left for the pointer line to the detailed note.
POINTER_ROOM = 200


@dataclass(frozen=True, slots=True)
class Usage:
    name: str  # "profile.md" / "soul.md"
    chars: int
    limit: int

    @property
    def over(self) -> bool:
        return self.chars > self.limit

    def line(self) -> str:
        key = "profile.usage_over" if self.over else "profile.usage"
        return t(key, name=self.name, chars=self.chars, limit=self.limit,
                 percent=round(100 * self.chars / max(1, self.limit)))


def _chars(path: Path) -> int:
    try:
        return len(path.read_text(encoding="utf-8"))
    except OSError:
        return 0


def usage(services: Services) -> list[Usage]:
    s = services.settings
    return [
        Usage("profile.md", _chars(s.profile_file), s.profile_max_chars),
        Usage("soul.md", _chars(s.soul_file), s.soul_max_chars),
    ]


# Shrink rounds after the first rewrite, when it is still over the limit.
SHRINK_ROUNDS = 3
# Aim this much under the limit: models overshoot character counts.
AIM = 0.9


LINE_CHARS = 75  # a dense bullet fact, for the line budget given to the model


def consolidate_prompt(aim: int, limit: int) -> str:
    lines = max(5, aim // LINE_CHARS)
    return (
        "You rewrite a personal profile that goes into every prompt of a personal assistant. "
        f"Write the CORE of it in about {aim} characters, and never more than {limit} characters "
        f"(count them: characters, not words): at most {lines} lines of at most 80 characters "
        "each. Keep only lasting facts: who the user is, family and close people, work, where he "
        "lives, lasting preferences and how he wants to be treated. Write dense bullet facts, one "
        "short line each, under a few one-word headings; no prose, no examples, no repetition. Put "
        "the most important facts first: the last lines may be cut. Drop what is outdated, trivial "
        "or one-off (old dates, past plans, amounts). Keep the profile's language (Portuguese). Do "
        "not invent anything. Two facts that contradict each other: keep the newer one, in place; "
        "never keep both. Answer with only the new profile text."
    )


def shrink_request(text: str, aim: int, limit: int) -> str:
    chars, lines = len(text), max(1, len(text.splitlines()))
    wanted = max(5, int(lines * aim / max(chars, 1)))
    return (
        f"That has {chars} characters in {lines} lines; the limit is {limit}. Cut at least "
        f"{chars - aim} characters (aim for about {aim}): rewrite it with at most {wanted} lines, "
        "merging bullets and dropping the least important facts and any wording that is not a "
        "fact. Answer with only the shorter profile."
    )


def fit_lines(text: str, limit: int) -> tuple[str, int]:
    """Keep whole lines from the top (the most important first) within ``limit``
    characters; returns (text, lines dropped). A heading left without lines goes too."""
    kept: list[str] = []
    size = 0
    lines = text.splitlines()
    for line in lines:
        if size + len(line) + 1 > limit:
            break
        kept.append(line)
        size += len(line) + 1
    while kept and (not kept[-1].strip() or kept[-1].lstrip().startswith("#")):
        kept.pop()
    return "\n".join(kept).strip(), len(lines) - len(kept)


async def consolidate(services: Services) -> str:
    """Shrink data/profile.md to PROFILE_MAX_CHARS. Returns the text for the user."""
    s = services.settings
    path = s.profile_file
    try:
        original = path.read_text(encoding="utf-8")
    except OSError:
        return t("profile.consolidate.missing")
    limit = s.profile_max_chars
    from uva.clock import utcnow

    today = utcnow().astimezone(services.tz).date()
    cleaned, pruned = preferences.drop_superseded(original, today)
    if len(cleaned) <= limit:
        if not pruned:
            return t("profile.consolidate.fits", chars=len(original), limit=limit)
        backup = _backup(path, original)
        path.write_text(cleaned, encoding="utf-8")
        await emit(EventType.SELF_EDIT, op="profile_consolidate", path="data/profile.md", ok=True,
                   before=len(original), after=len(cleaned), backup=backup, pruned=pruned)
        return t("profile.consolidate.pruned", count=pruned, chars=len(cleaned), limit=limit, backup=backup)
    if services.llm is None:
        return t("profile.consolidate.no_llm")
    if services.budget is not None:
        status = await services.budget.check(Scope.INTERACTIVE)
        if not status.allowed:
            return t("profile.consolidate.no_budget", reason=status.reason)

    # The Preferences section is kept as is (its lines carry their markers); the
    # rest of the profile is rewritten around it.
    rest, section = preferences.split_section(cleaned)
    target = max(500, limit - POINTER_ROOM - (len(section) + 2 if section else 0))
    aim = int(target * AIM)
    messages = [LLMMessage("user", rest)]
    best = ""  # the shortest non-empty rewrite so far
    rounds = 0
    for rounds in range(1, SHRINK_ROUNDS + 2):
        try:
            response = await services.llm.complete(
                messages,
                purpose="profile_consolidate",
                scope=Scope.INTERACTIVE,
                model=services.llm.heavy_model,
                system=consolidate_prompt(aim, target),
                use_soul=False,
            )
        except LLMError as exc:
            if best and len(best) <= target:
                break
            return t("profile.consolidate.failed", error=exc)
        core = response.text.strip()
        if core and (not best or len(core) < len(best)):
            best = core
        if best and len(best) <= target:
            break
        messages += [
            LLMMessage("assistant", response.text),
            LLMMessage("user", shrink_request(core, aim, target)),
        ]
    dropped = 0
    if best and len(best) > target:
        # Still too long after every round: drop the last (least important) lines.
        # Nothing is lost: the full text goes to data/.versions/ and the brain note.
        best, dropped = fit_lines(best, target)
    if not best:
        return t("profile.consolidate.too_long", chars=0, limit=target, rounds=rounds)
    lost = preferences.loss(rest, best)
    if lost > s.profile_max_loss:
        await emit(EventType.SELF_EDIT, op="profile_consolidate", path="data/profile.md", ok=False,
                   refused="loss", loss=round(lost, 2))
        return t("profile.consolidate.too_lossy", percent=round(100 * lost), limit=round(100 * s.profile_max_loss))
    core = best + (f"\n\n{section}" if section else "")

    # The full text first, so nothing is lost even if the rest fails.
    backup = _backup(path, original)
    detailed = _save_detailed(services, original)
    pointer = t("profile.consolidate.pointer", lang="pt", note=detailed) if detailed else ""
    new_text = core + (f"\n\n{pointer}" if pointer else "") + "\n"
    path.write_text(new_text, encoding="utf-8")
    await emit(EventType.SELF_EDIT, op="profile_consolidate", path="data/profile.md", ok=True,
               before=len(original), after=len(new_text), backup=backup, note=detailed,
               rounds=rounds, dropped_lines=dropped)
    done = t("profile.consolidate.done", before=len(original), after=len(new_text), limit=limit,
             backup=backup, note=detailed or "—")
    return done + ("\n" + t("profile.consolidate.trimmed", lines=dropped) if dropped else "")


def _backup(path: Path, text: str) -> str:
    versions = path.parent / ".versions"
    versions.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = versions / f"{path.stem}-{stamp}{path.suffix}"
    backup.write_text(text, encoding="utf-8")
    return f"data/.versions/{backup.name}"


def _save_detailed(services: Services, text: str) -> str | None:
    """The full profile as a brain note (his content: found by recall, never always-loaded)."""
    vault = services.vault
    if vault is None:
        return None
    try:
        try:
            note, _ = vault.update_note(DETAILED_PATH, body=text)
        except LookupError:
            note = vault.create_note(
                DETAILED_TITLE, text, folder=DETAILED_FOLDER, subfolder=DETAILED_SUBFOLDER, tags=["perfil"]
            )
        return note.path
    except Exception as exc:
        log.warning("profile_detailed_note_failed", error=str(exc))
        return None
