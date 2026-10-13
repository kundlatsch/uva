"""Bring existing notes to the naming pattern: Portuguese title, snake_case file.

Run once by /brain renomear. Titles that read as English get a Portuguese one
(cheap LLM call); files are renamed, the front matter title and H1 updated,
attachments renamed too, and every wiki link in the vault rewritten. The
brain's own git repository gets one commit, so /reverter brain undoes it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from uva.brain.naming import file_stem, link, looks_portuguese, portuguese_title
from uva.brain.vault import FOLDERS, README_NAME, Vault, parse_note

SKIP_FOLDERS = (FOLDERS["diario"],)  # daily notes are named by date already
KEEP_PREFIXES = ("Pesquisa - ", "Objetivo - ")


@dataclass
class Rename:
    old: str  # relative paths
    new: str
    old_title: str
    new_title: str


def _render(meta: dict, body: str) -> str:
    front = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).strip()
    return f"---\n{front}\n---\n\n{body.lstrip()}"


def _free(path: Path, taken: set[Path]) -> Path:
    candidate, n = path, 2
    while (candidate.exists() and candidate not in taken) or candidate in taken:
        candidate = path.with_name(f"{path.stem}_{n}{path.suffix}")
        n += 1
    return candidate


async def plan_renames(services, vault: Vault) -> list[Rename]:
    root = vault.root
    renames: list[Rename] = []
    taken: set[Path] = set()
    for path in sorted(root.rglob("*.md")):
        rel = path.relative_to(root).as_posix()
        if not vault._visible(path) or path.name == README_NAME or rel.startswith(SKIP_FOLDERS):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if not text.strip():  # empty folder markers (folder_create): keep the folder's name
            continue
        meta, body = parse_note(text)
        title = str(meta.get("title") or path.stem)
        new_title = title
        if not looks_portuguese(title):
            new_title = await portuguese_title(services, title, body)
            prefix = next((p for p in KEEP_PREFIXES if title.startswith(p)), "")
            if prefix and not new_title.startswith(prefix):
                new_title = prefix + new_title  # research notes stay "Pesquisa - ..."
        target = path.with_name(f"{file_stem(new_title)}.md")
        if target == path and new_title == title:
            continue
        target = target if target == path else _free(target, taken)
        taken.add(target)
        renames.append(Rename(rel, target.relative_to(root).as_posix(), title, new_title))
    return renames


def apply_renames(vault: Vault, renames: list[Rename]) -> dict[str, str]:
    """Move the files, fix titles and links. Returns {old attachment: new} too."""
    root = vault.root
    by_old_stem: dict[str, Rename] = {}
    for r in renames:
        by_old_stem[Path(r.old).stem] = r
        by_old_stem[r.old_title] = r
    # Attachments: snake_case like the notes.
    attachments: dict[str, str] = {}
    folder = root / FOLDERS["anexos"]
    if folder.is_dir():
        for path in sorted(folder.iterdir()):
            if path.is_file():
                new = path.with_name(f"{file_stem(path.stem)}{path.suffix.lower()}")
                if new != path:
                    new = _free(new, set())
                    path.rename(new)
                    attachments[path.relative_to(root).as_posix()] = new.relative_to(root).as_posix()

    for r in renames:
        src, dst = root / r.old, root / r.new
        meta, body = parse_note(src.read_text(encoding="utf-8"))
        if r.new_title != r.old_title:
            meta["title"] = r.new_title
            body = re.sub(rf"^# {re.escape(r.old_title)}\s*$", f"# {r.new_title}", body, count=1, flags=re.MULTILINE)
        src.write_text(_render(meta, body) if meta else body, encoding="utf-8")
        if dst != src:
            src.rename(dst)

    pattern = re.compile(r"(!?)\[\[([^\]|#]+)(#[^\]|]*)?(?:\|([^\]]*))?\]\]")

    def fix(match: re.Match) -> str:
        bang, target, anchor, alias = match.group(1), match.group(2).strip(), match.group(3) or "", match.group(4)
        if bang and target in attachments:
            return f"![[{attachments[target]}{anchor}]]"
        rename = by_old_stem.get(target) or by_old_stem.get(Path(target).stem)
        if rename is None:
            return match.group(0)
        shown = alias if alias and alias != rename.old_title else rename.new_title
        return link(Path(rename.new).stem, shown).replace("]]", f"{anchor}]]") if anchor else link(Path(rename.new).stem, shown)

    for path in root.rglob("*.md"):
        if not vault._visible(path):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        fixed = pattern.sub(fix, text)
        if fixed != text:
            path.write_text(fixed, encoding="utf-8")
    return attachments
