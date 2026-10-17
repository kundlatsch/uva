"""Stickers: the bot learns what each one means, and actions tied to them.

Two separate things, both kept in the SQLite table ``stickers`` (key: the
sticker's Telegram file_unique_id):

* meaning: the first time a sticker shows up alone, the bot asks what it means
  and the next message becomes the meaning. From then on, sending it is
  understood as "[sticker 😂: <meaning>]", with the conversation context and
  no image interpretation;
* action: "toda vez que eu mandar essa figurinha, <faça X>" (or "every time I
  send this sticker, <do X>") ties a request to the sticker; every time it is
  sent, the request goes through the normal router (same budget and safeguards as
  if it had been typed).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from uva.clock import utcnow
from uva.i18n import t
from uva.storage.stickers import Sticker

QUESTION_TTL = timedelta(minutes=30)
FOCUS_TTL = timedelta(minutes=15)

ACTION_RE = re.compile(
    r"^\s*(?:e\s+)?(?:toda\s+vez|sempre|quando)\s+(?:que\s+)?(?:eu\s+)?(?:te\s+)?(?:mandar|enviar)\s+"
    r"(?:essa|esta|a|aquela|ess[ae]\s+)?\s*figurinha\s*[,:]?\s*(?P<action>.+)$",
    re.IGNORECASE | re.DOTALL,
)
# The same in English: "every time I send this sticker, tell me a joke".
ACTION_RE_EN = re.compile(
    r"^\s*(?:and\s+)?(?:every\s*time|whenever|when)\s+(?:i\s+)?(?:send|post)\s+(?:you\s+)?"
    r"(?:this|that|the)?\s*sticker\s*[,:]?\s*(?P<action>.+)$",
    re.IGNORECASE | re.DOTALL,
)
SKIP = (
    "/cancelar", "/pular", "deixa", "deixa pra la", "deixa pra lá", "esquece", "nada",
    "/cancel", "/skip", "skip", "never mind", "nevermind", "forget it", "nothing",
)


def action_request(text: str) -> str | None:
    """'toda vez que eu mandar essa figurinha, me manda a previsão' -> 'me manda a previsão'."""
    match = ACTION_RE.match(text) or ACTION_RE_EN.match(text)
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
    return f"[sticker {sticker.emoji}: {sticker.meaning}]".replace("  ", " ")


def action_message(sticker: Sticker) -> str:
    """What the router receives when a sticker with an action is sent."""
    if sticker.action.startswith("/"):
        return sticker.action
    about = f" (meaning: {sticker.meaning})" if sticker.meaning else ""
    return f"{sticker.action}\n[request tied to the sticker {sticker.emoji}{about} I just sent]"


def describe(sticker: Sticker) -> str:
    header = t("stickers.describe.header", label=sticker.label, uses=sticker.uses)
    if sticker.set_name:
        header += t("stickers.describe.set", name=sticker.set_name)
    lines = [header]
    meaning = sticker.meaning or t("stickers.describe.no_meaning")
    lines.append(t("stickers.describe.meaning", meaning=meaning))
    if sticker.action:
        lines.append(t("stickers.describe.action", action=sticker.action))
    return "\n".join(lines)
