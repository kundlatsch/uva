"""Photos received from Telegram, as base64 data URLs (what Decisions accepts)."""

from __future__ import annotations

import base64
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta

from uva.clock import utcnow

LAST_PHOTO_TTL = timedelta(minutes=30)

# Photo attached to the message being handled right now (caption = the text).
current_photo: ContextVar[str | None] = ContextVar("uva_current_photo", default=None)


def to_data_url(data: bytes, mime: str = "image/jpeg") -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def parse_data_url(url: str) -> tuple[str, str]:
    """"data:image/png;base64,AAA" -> ("image/png", "AAA")."""
    header, _, data = url.partition(",")
    mime = header.removeprefix("data:").split(";")[0] or "image/jpeg"
    return mime, data


def data_url_bytes(url: str) -> tuple[str, bytes]:
    mime, data = parse_data_url(url)
    return mime, base64.b64decode(data)


EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}

# Words that point at "the picture I just sent" in a follow-up message.
IMAGE_HINTS = (
    "foto", "imagem", "print", "figura", "tela", "escrito", "escrita", "traduz", "traduza",
    "le isso", "leia", "o que diz", "o que tem", "aparece", "nessa", "nesta", "dela",
)


def mentions_image(normalized_text: str) -> bool:
    padded = f" {normalized_text} "
    return any(f" {hint} " in padded for hint in IMAGE_HINTS)


@dataclass(slots=True)
class LastPhoto:
    """The most recent photo the user sent (for "e essa foto?" follow-ups)."""

    data_url: str | None = None
    at: datetime | None = None

    def set(self, data_url: str) -> None:
        self.data_url, self.at = data_url, utcnow()

    def get(self) -> str | None:
        if self.data_url and self.at and utcnow() - self.at <= LAST_PHOTO_TTL:
            return self.data_url
        return None
