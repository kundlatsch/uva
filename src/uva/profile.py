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


def consolidate_prompt(limit: int) -> str:
    return (
        "You rewrite a personal profile that goes into every prompt of a personal assistant. "
        f"Write the CORE of it in at most {limit} characters: who the user is, family and close "
        "people, work, where he lives, lasting preferences and how he wants to be treated. Merge "
        "repetitions, drop what is outdated, trivial or one-off (old dates, past plans, amounts). "
        "Keep the profile's language (Portuguese) and Markdown headings/bullets. Do not invent "
        "anything. Answer with only the new profile text."
    )


async def consolidate(services: Services) -> str:
    """Shrink data/profile.md to PROFILE_MAX_CHARS. Returns the text for the user."""
    s = services.settings
    path = s.profile_file
    try:
        original = path.read_text(encoding="utf-8")
    except OSError:
        return t("profile.consolidate.missing")
    limit = s.profile_max_chars
    if len(original) <= limit:
        return t("profile.consolidate.fits", chars=len(original), limit=limit)
    if services.llm is None:
        return t("profile.consolidate.no_llm")
    if services.budget is not None:
        status = await services.budget.check(Scope.INTERACTIVE)
        if not status.allowed:
            return t("profile.consolidate.no_budget", reason=status.reason)

    target = max(500, limit - POINTER_ROOM)
    messages = [LLMMessage("user", original)]
    core = ""
    for _ in range(2):
        try:
            response = await services.llm.complete(
                messages,
                purpose="profile_consolidate",
                scope=Scope.INTERACTIVE,
                model=services.llm.heavy_model,
                system=consolidate_prompt(target),
                use_soul=False,
            )
        except LLMError as exc:
            return t("profile.consolidate.failed", error=exc)
        core = response.text.strip()
        if 0 < len(core) <= target:
            break
        messages += [
            LLMMessage("assistant", response.text),
            LLMMessage("user", f"That has {len(core)} characters. Shorten it to under {target}."),
        ]
    if not core or len(core) > target:
        return t("profile.consolidate.too_long", chars=len(core), limit=target)

    # The full text first, so nothing is lost even if the rest fails.
    backup = _backup(path, original)
    detailed = _save_detailed(services, original)
    pointer = t("profile.consolidate.pointer", lang="pt", note=detailed) if detailed else ""
    new_text = core + (f"\n\n{pointer}" if pointer else "") + "\n"
    path.write_text(new_text, encoding="utf-8")
    await emit(EventType.SELF_EDIT, op="profile_consolidate", path="data/profile.md", ok=True,
               before=len(original), after=len(new_text), backup=backup, note=detailed)
    return t("profile.consolidate.done", before=len(original), after=len(new_text), limit=limit,
             backup=backup, note=detailed or "—")


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
