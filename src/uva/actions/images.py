"""image_save: keep the last picture in the second brain, with a note that embeds it."""

from __future__ import annotations

import asyncio

from pydantic import BaseModel, ConfigDict, Field

from uva.brain.naming import portuguese_title, require_title_pt
from uva.actions.registry import ActionContext, ActionRegistry, ActionResult
from uva.brain.vault import title_from_text
from uva.clock import format_local, utcnow
from uva.media import EXTENSIONS, data_url_bytes
from uva.stm.refs import remember_ref


class ImageSaveParams(BaseModel):
    model_config = ConfigDict(json_schema_extra=require_title_pt)

    title_pt: str = Field(
        default="",
        description=(
            "REQUIRED. Title in Brazilian Portuguese (it is the user's content): becomes the file "
            "name (snake_case, no accents) and the note's title. Never English."
        ),
    )
    title: str = Field(default="", description="(deprecated: use title_pt)")
    description: str = Field(default="", description="O que a imagem mostra (texto da nota)")
    folder: str = Field(default="inbox", description="Pasta da nota: inbox, projetos, areas, recursos")


def register(registry: ActionRegistry) -> None:
    @registry.action(
        "image_save",
        "Salva a última imagem que o usuário mandou no second brain, com uma nota que a mostra",
        ImageSaveParams,
    )
    async def image_save(params: ImageSaveParams, ctx: ActionContext) -> ActionResult:
        services = ctx.services
        if services.vault is None:
            return ActionResult(text="O second brain não está configurado.")
        photo = services.last_photo.get()
        if photo is None:
            return ActionResult(text="Não tenho imagem recente para salvar (manda de novo).")
        mime, data = data_url_bytes(photo)
        when = format_local(utcnow(), services.tz, "%Y-%m-%d %H%M")
        title = (params.title_pt or params.title).strip()
        if title or params.description.strip():
            title = await portuguese_title(services, title, params.description, scope=ctx.scope)
        title = title or f"Imagem {when}"
        folder = params.folder if params.folder in ("inbox", "projetos", "areas", "recursos") else "inbox"

        vault = services.vault
        attachment = await asyncio.to_thread(vault.save_attachment, data, EXTENSIONS.get(mime, "jpg"), title)
        body = f"![[{attachment}]]"
        if params.description.strip():
            body += f"\n\n{params.description.strip()}"
        note = vault.create_note(title, body, folder=folder, tags=["imagem"])
        vault.append_daily(f"Imagem salva: {note.link}")
        await remember_ref(services, "nota", note.path, f"nota: {note.title}")
        return ActionResult(
            text=f"🖼️ Imagem salva em {attachment}\n📝 Nota: {note.path}",
            data={"attachment": attachment, "note": note.path},
        )
