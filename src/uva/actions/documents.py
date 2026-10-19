"""document_read: the rest of a document the user sent (its note in 05-anexos)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from uva.actions.registry import ActionContext, ActionRegistry, ActionResult
from uva.brain.vault import FOLDERS
from uva.i18n import t


class DocumentReadParams(BaseModel):
    path: str = Field(description="The document's note, as given in the context (05-anexos/...)")
    offset: int = Field(default=0, description="Character to start at (0 = the beginning)")


def register(registry: ActionRegistry) -> None:
    @registry.action(
        "document_read",
        "Reads a document the user sent, from a character offset (8000 characters per call)",
        DocumentReadParams,
        lookup=True,
        proactive_safe=True,
    )
    async def document_read(params: DocumentReadParams, ctx: ActionContext) -> ActionResult:
        from uva import documents

        vault = ctx.services.vault
        path = params.path.strip().lstrip("/")
        if vault is None or not path.startswith(FOLDERS["anexos"] + "/"):
            return ActionResult(text=t("actions.documents.not_a_document", path=params.path))
        try:
            note = vault.read(path)
        except LookupError:
            return ActionResult(text=t("actions.documents.not_a_document", path=params.path))
        text = documents.text_of(note.body)
        chunk, following = documents.window(text, params.offset)
        if following is not None:
            more = t("actions.documents.continues", offset=following, total=len(text))
        else:
            more = t("actions.documents.end")
        return ActionResult(
            text=f"{chunk}\n{more}",
            data={"path": path, "offset": params.offset, "next": following, "total": len(text)},
        )
