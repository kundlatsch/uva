"""The "## Relacionadas" section of the notes UVA creates (research, goal reports,
documents): up to 3 [[links]] to the notes most like it in the search index
(bm25 over its title and the first 2000 characters), refreshed when the note
is rewritten. Best effort: a missing index just means no section.
"""

from __future__ import annotations

from pathlib import PurePosixPath

from uva.brain.naming import link
from uva.brain.vault import ARCHIVE_PREFIX, FOLDERS, RELATED
from uva.logging import get_logger

log = get_logger(__name__)

MAX_RELATED = 3
BODY_CHARS = 2000
MAX_TERMS = 12
SKIP = (ARCHIVE_PREFIX, FOLDERS["diario"] + "/")


async def refresh_related(services, path: str) -> list[str]:
    """Rewrite the note's related links; returns them (best effort)."""
    vault, index = services.vault, services.index
    if vault is None or index is None or not path:
        return []
    from uva.recall import terms

    try:
        note = vault.read(path)
        body = RELATED.sub("", note.body)[:BODY_CHARS]
        words = terms(f"{note.title} {body}")[:MAX_TERMS]
        if not words:
            return []
        links = []
        for hit in await index.search(words, kinds=("note",), limit=MAX_RELATED * 4):
            if hit.ref == note.path or hit.ref.startswith(SKIP):
                continue
            links.append(link(PurePosixPath(hit.ref).stem, hit.title))
            if len(links) >= MAX_RELATED:
                break
        vault.set_related(note.path, links)
        return links
    except Exception as exc:
        log.warning("related_failed", path=path, error=str(exc))
        return []
