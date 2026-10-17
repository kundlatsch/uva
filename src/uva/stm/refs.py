"""Recording what the bot did, from anywhere (commands, actions, agent)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from uva.logging import get_logger

if TYPE_CHECKING:
    from uva.services import Services

log = get_logger(__name__)


def task_label(action: str, params: dict) -> str:
    """Label of a task's ref ("search: Fórmula 1"): data for the LLM, always English."""
    subject = params.get("query") or params.get("title") or ""
    return f"{action}: {subject}" if subject else action


async def remember_ref(services: Services, tipo, id: str, rotulo: str, status="ok") -> None:
    """Add a reference to the short-term memory (best effort).

    ``rotulo`` is data the LLM reads, so it is English in any language
    ("note: Compras", "expense: R$ 12,00 mercado")."""
    if services.stm is None:
        return
    try:
        await services.stm.add_ref(tipo, id, rotulo, status)
    except Exception as exc:
        log.warning("stm_ref_failed", error=str(exc))
