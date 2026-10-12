"""Common Markdown (from the LLM, Claude Code, notes) -> Telegram HTML.

Telegram's own Markdown modes break on ordinary Markdown (unbalanced "_",
"**bold**", headings...). Everything is HTML-escaped first, then only a few
well-formed constructs become tags. If Telegram still rejects the result,
the sender falls back to plain text.
"""

from __future__ import annotations

import html
import re

FENCE = re.compile(r"```[^\n`]*\n?(.*?)```", re.DOTALL)
INLINE_CODE = re.compile(r"`([^`\n]+)`")
LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*|__(?=\S)(.+?)(?<=\S)__")
ITALIC = re.compile(r"(?<![\w*])\*(?=[^\s*])([^*\n]+?)(?<=[^\s*])\*(?![\w*])|(?<![\w_])_(?=[^\s_])([^_\n]+?)(?<=[^\s_])_(?![\w_])")
STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")
HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", re.MULTILINE)
BULLET = re.compile(r"^(\s*)[-*+]\s+", re.MULTILINE)
QUOTE = re.compile(r"^&gt;\s?(.*)$", re.MULTILINE)
RULE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$", re.MULTILINE)


def to_html(text: str) -> str:
    """Telegram HTML for ``text``; code is kept verbatim, everything else escaped."""
    stash: list[str] = []

    def keep(fragment: str) -> str:
        stash.append(fragment)
        return f"\x00{len(stash) - 1}\x00"

    # Code first, so nothing inside it is interpreted.
    text = FENCE.sub(lambda m: keep(f"<pre>{html.escape(m.group(1).rstrip())}</pre>"), text)
    text = INLINE_CODE.sub(lambda m: keep(f"<code>{html.escape(m.group(1))}</code>"), text)
    text = LINK.sub(lambda m: keep(f'<a href="{html.escape(m.group(2), quote=True)}">{html.escape(m.group(1))}</a>'), text)

    text = html.escape(text, quote=False)
    text = RULE.sub("──────────", text)
    text = HEADING.sub(lambda m: f"<b>{m.group(1)}</b>", text)
    text = BULLET.sub(lambda m: f"{m.group(1)}• ", text)
    text = QUOTE.sub(lambda m: f"<blockquote>{m.group(1)}</blockquote>", text)
    text = text.replace("</blockquote>\n<blockquote>", "\n")
    text = BOLD.sub(lambda m: f"<b>{m.group(1) or m.group(2)}</b>", text)
    text = STRIKE.sub(lambda m: f"<s>{m.group(1)}</s>", text)
    text = ITALIC.sub(lambda m: f"<i>{m.group(1) or m.group(2)}</i>", text)

    return re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], text)


def to_plain(text: str) -> str:
    """Fallback: the same text without Markdown markers."""
    text = FENCE.sub(lambda m: m.group(1).rstrip(), text)
    text = INLINE_CODE.sub(lambda m: m.group(1), text)
    text = LINK.sub(lambda m: f"{m.group(1)} ({m.group(2)})", text)
    text = HEADING.sub(lambda m: m.group(1), text)
    text = BOLD.sub(lambda m: m.group(1) or m.group(2), text)
    return text
