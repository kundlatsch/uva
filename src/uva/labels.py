"""Turn labels: how a turn that is not plain text is kept and shown later.

They are what the short-term memory, the conversation log, the event log and
the turn tracker keep for a photo, an audio, a button press, a sticker, a
document or a forward, so ``/contexto``, ``/historico``, ``/log``, ``/parar``
and the dashboard show them in the bot's language (``locales/*/labels.json``).
"""

from __future__ import annotations

from uva.i18n import SUPPORTED, t


def label(kind: str, **values: object) -> str:
    return t(f"labels.{kind}", **values)


def voice_text(text: str) -> str | None:
    """The transcript in a voice turn's label (written in any language), or None."""
    for lang in SUPPORTED:
        prefix = t("labels.voice", lang=lang, text="")
        if text.startswith(prefix):
            return text[len(prefix):]
    return None
