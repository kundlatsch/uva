"""/nota and /buscar: quick access to the second brain."""

from __future__ import annotations

import asyncio

from uva.brain.vault import title_from_text
from uva.commands.registry import CommandContext, CommandRegistry

SEARCH_LIMIT = 10


def register(registry: CommandRegistry) -> None:
    @registry.command(
        "nota",
        "Salva uma nota rápida no inbox do second brain",
        aliases=("anota", "anotar"),
        usage="<texto>",
    )
    async def note(ctx: CommandContext) -> str:
        vault = ctx.services.vault
        if vault is None:
            return "O second brain não está configurado."
        text = " ".join(ctx.args).strip()
        if not text:
            return "Uso: /nota <texto>"
        saved = vault.create_note(title_from_text(text), text, folder="inbox", tags=["inbox"])
        vault.append_daily(f"Nota rápida: [[{saved.title}]]")
        return f"📝 Nota salva: {saved.path}"

    @registry.command(
        "buscar",
        "Procura no second brain",
        aliases=("procurar",),
        usage="<termo>",
        examples=("procura nas notas",),
    )
    async def search(ctx: CommandContext) -> str:
        vault = ctx.services.vault
        if vault is None:
            return "O second brain não está configurado."
        term = " ".join(ctx.args).strip()
        if not term:
            return "Uso: /buscar <termo>"
        hits = await asyncio.to_thread(vault.search, term, SEARCH_LIMIT)
        if not hits:
            return f"{ctx.services.say('sem_resultado', termo=term)}\n(nenhuma nota com '{term}')"
        return f"Notas com '{term}':\n" + "\n".join(f"- {h.path}\n  {h.snippet}" for h in hits)
