"""Names in the second brain: Portuguese titles, snake_case file names.

The brain is the user's content, so titles are in Portuguese even though the
bot works in English internally. A note titled "Marmitas Frias e
Requentadas" is stored as ``marmitas_frias_e_requentadas.md``; the title, with
accents, stays in the front matter and the H1, and wiki links are written as
``[[marmitas_frias_e_requentadas|Marmitas Frias e Requentadas]]``.
"""

from __future__ import annotations

import re
import unicodedata

MAX_STEM = 80

PT_WORDS = frozenset(
    "a o as os de da do das dos e em no na nos nas um uma para pra por com sem que se sobre "
    "como mais ou ao aos à às pesquisa notícias noticias últimas ultimas história historia "
    "previsão tempo hoje dia dias livro livros nota notas objetivo imagem desenho casa".split()
)
EN_WORDS = frozenset(
    "the and of to in on for with what is are how latest updates news weather conditions "
    "food about history book books summary today forecast check find search".split()
)


def require_title_pt(schema: dict) -> None:
    """json_schema_extra: show title_pt as required to the LLM (a missing one still
    validates, and gets a Portuguese title from portuguese_title)."""
    schema.setdefault("required", [])
    if "title_pt" not in schema["required"]:
        schema["required"].append("title_pt")


def file_stem(title: str) -> str:
    """'Marmitas Frias e Requentadas' -> 'marmitas_frias_e_requentadas'."""
    text = unicodedata.normalize("NFKD", title)
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    text = text[:MAX_STEM].rstrip("_")
    return text or "sem_titulo"


def link(stem: str, title: str) -> str:
    """Obsidian wiki link to a note, showing its title."""
    return f"[[{stem}]]" if stem == title else f"[[{stem}|{title}]]"


def looks_portuguese(text: str) -> bool:
    """Rough check: Portuguese words/accents vs English words."""
    words = re.findall(r"[a-zà-ü]+", text.lower())
    pt = sum(w in PT_WORDS for w in words) + len(re.findall(r"[áàâãéêíóôõúç]", text, re.IGNORECASE))
    en = sum(w in EN_WORDS for w in words)
    # "Prêmio Jabuti 2026 latest updates": an accent doesn't make English words Portuguese.
    return en == 0 or (en == 1 and pt >= 2) or pt > en * 2


async def portuguese_title(
    services, candidate: str, context: str = "", *, scope=None, alternatives: tuple[str, ...] = ()
) -> str:
    """The first of ``candidate``/``alternatives`` that reads as Portuguese; otherwise a
    short Portuguese title from a cheap LLM call (or a heuristic from the note text)."""
    candidates = [c.strip() for c in (candidate or "", *alternatives) if c and c.strip()]
    for option in candidates:
        if looks_portuguese(option):
            return option
    candidate = candidates[0] if candidates else ""
    llm = getattr(services, "llm", None)
    if llm is not None:
        from uva.llm.base import LLMMessage
        from uva.llm.budget import Scope

        material = f"Suggested title: {candidate or '(none)'}\n\nNote:\n{context[:1500]}"
        try:
            response = await llm.complete(
                [LLMMessage("user", material)],
                purpose="note_title",
                scope=scope or Scope.INTERACTIVE,
                system=(
                    "Give a short, clear title (at most 8 words) in Brazilian Portuguese for this "
                    "note in the user's second brain. Answer with the title only."
                ),
                max_tokens=40,
                use_soul=False,
            )
            title = response.text.strip().strip('"').splitlines()[0].strip() if response.text.strip() else ""
            if title:
                return title[:MAX_STEM]
        except Exception:
            pass
    first_line = next((line.strip("# ").strip() for line in context.splitlines() if line.strip()), "")
    if first_line and looks_portuguese(first_line):
        return " ".join(first_line.split()[:8])
    return candidate or "Nota sem título"
