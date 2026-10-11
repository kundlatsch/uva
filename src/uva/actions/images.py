"""image_save: keep the last picture in the second brain, with a note that embeds it."""

from __future__ import annotations

import asyncio

from pydantic import BaseModel, Field

from uva.actions.registry import ActionContext, ActionRegistry, ActionResult
from uva.clock import format_local, utcnow
from uva.media import EXTENSIONS, data_url_bytes
from uva.stm.refs import remember_ref


class ImageSaveParams(BaseModel):
    title: str = Field(default="", description="Título curto para a nota/arquivo")
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
        title = params.title.strip() or f"Imagem {when}"
        folder = params.folder if params.folder in ("inbox", "projetos", "areas", "recursos") else "inbox"

        vault = services.vault
        attachment = await asyncio.to_thread(vault.save_attachment, data, EXTENSIONS.get(mime, "jpg"), title)
        body = f"![[{attachment}]]"
        if params.description.strip():
            body += f"\n\n{params.description.strip()}"
        note = vault.create_note(title, body, folder=folder, tags=["imagem"])
        vault.append_daily(f"Imagem salva: [[{note.title}]]")
        await remember_ref(services, "nota", note.path, f"nota: {note.title}")
        return ActionResult(
            text=f"🖼️ Imagem salva em {attachment}\n📝 Nota: {note.path}",
            data={"attachment": attachment, "note": note.path},
        )
