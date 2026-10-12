"""Stickers: the bot learns what each one means, and actions tied to them.

Two separate things, both kept in the SQLite table ``stickers`` (key: the
sticker's Telegram file_unique_id):

* meaning: the first time a sticker shows up alone, the bot asks what it means
  and the next message becomes the meaning. From then on, sending it is
  understood as "[figurinha 😂: <meaning>]", with the conversation context and
  no image interpretation;
* action: "toda vez que eu mandar essa figurinha, <faça X>" ties a request to
  the sticker; every time it is sent, the request goes through the normal
  router (same budget and safeguards as if it had been typed).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from uva.clock import utcnow
from uva.storage.stickers import Sticker

QUESTION_TTL = timedelta(minutes=30)
FOCUS_TTL = timedelta(minutes=15)

ACTION_RE = re.compile(
    r"^\s*(?:e\s+)?(?:toda\s+vez|sempre|quando)\s+(?:que\s+)?(?:eu\s+)?(?:te\s+)?(?:mandar|enviar)\s+"
    r"(?:essa|esta|a|aquela|ess[ae]\s+)?\s*figurinha\s*[,:]?\s*(?P<action>.+)$",
    re.IGNORECASE | re.DOTALL,
)
SKIP = ("/cancelar", "/pular", "deixa", "deixa pra la", "deixa pra lá", "esquece", "nada")


def action_request(text: str) -> str | None:
    """'toda vez que eu mandar essa figurinha, me manda a previsão' -> 'me manda a previsão'."""
    match = ACTION_RE.match(text)
    if not match:
        return None
    action = match.group("action").strip().rstrip(".")
    return action or None


@dataclass
class StickerFocus:
    """The sticker the conversation is about (bot process memory)."""

    unique_id: str
    emoji: str
    at: datetime
    asking: bool  # waiting for its meaning

    def fresh(self, now: datetime | None = None) -> bool:
        now = now or utcnow()
        return now - self.at < (QUESTION_TTL if self.asking else FOCUS_TTL)


def as_message(sticker: Sticker) -> str:
    """How a known sticker is shown to the LLM."""
    return f"[figurinha {sticker.emoji}: {sticker.meaning}]".replace("  ", " ")


def action_message(sticker: Sticker) -> str:
    """What the router receives when a sticker with an action is sent."""
    if sticker.action.startswith("/"):
        return sticker.action
    about = f" (que significa: {sticker.meaning})" if sticker.meaning else ""
    return f"{sticker.action}\n[pedido atrelado à figurinha {sticker.emoji}{about}, que acabei de mandar]"


def describe(sticker: Sticker) -> str:
    lines = [f"{sticker.label} · usada {sticker.uses}x" + (f" · pacote {sticker.set_name}" if sticker.set_name else "")]
    lines.append(f"   significado: {sticker.meaning or '(nenhum)'}")
    if sticker.action:
        lines.append(f"   ação: {sticker.action}")
    return "\n".join(lines)
