"""Recording what the bot did, from anywhere (commands, actions, agent)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from uva.logging import get_logger

if TYPE_CHECKING:
    from uva.services import Services

log = get_logger(__name__)


def task_label(action: str, params: dict) -> str:
    subject = params.get("query") or params.get("title") or ""
    kind = "pesquisa" if action == "search" else action
    return f"{kind}: {subject}" if subject else kind


async def remember_ref(services: Services, tipo, id: str, rotulo: str, status="ok") -> None:
    """Add a reference to the short-term memory (best effort)."""
    if services.stm is None:
        return
    try:
        await services.stm.add_ref(tipo, id, rotulo, status)
    except Exception as exc:
        log.warning("stm_ref_failed", error=str(exc))
