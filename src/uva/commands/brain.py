"""/nota, /buscar and /brain: quick access to the second brain."""

from __future__ import annotations

import asyncio
from pathlib import Path

from uva.brain.vault import Entry, NoteNotFoundError, Vault, title_from_text
from uva.clock import format_local
from uva.commands.registry import CommandContext, CommandRegistry
from uva.replies import Reply
from uva.stm.refs import remember_ref

SEARCH_LIMIT = 10
TREE_FILES_PER_FOLDER = 30  # the tree is paginated, so show plenty


def _size(entry: Entry) -> str:
    if entry.is_dir:
        return f"{entry.size} nota(s)"
    return f"{entry.size} B" if entry.size < 1024 else f"{entry.size / 1024:.1f} KB"


def render_listing(vault: Vault, folder: Path, tz) -> str:
    entries = vault.list_folder(folder)
    where = vault._relative(folder) if folder != vault.root.resolve() else "brain"
    if not entries:
        return f"📂 {where}: vazia."
    lines = [f"📂 {where}"]
    for entry in entries:
        name = entry.path.rsplit("/", 1)[-1]
        when = format_local(entry.modified, tz, "%d/%m %H:%M") if entry.modified else ""
        icon = "📁" if entry.is_dir else "📝"
        lines.append(f"{icon} {name}{'/' if entry.is_dir else ''} · {_size(entry)} · {when}")
    return "\n".join(lines)


def render_tree(vault: Vault) -> str:
    """Every PARA folder with its notes (first few per folder)."""
    lines = ["🧠 brain"]
    for entry in vault.list_folder(vault.root.resolve()):
        if not entry.is_dir:
            lines.append(f"📝 {entry.path}")
            continue
        lines.append(f"📁 {entry.path}/ ({entry.size} nota(s))")
        notes = sorted(
            (p for p in (vault.root / entry.path).rglob("*.md") if vault._visible(p)),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for path in notes[:TREE_FILES_PER_FOLDER]:
            lines.append(f"   · {vault._relative(path).split('/', 1)[1]}")
        if len(notes) > TREE_FILES_PER_FOLDER:
            lines.append(f"   … e mais {len(notes) - TREE_FILES_PER_FOLDER} (/brain {entry.path})")
    return "\n".join(lines)


def register(registry: CommandRegistry) -> None:
    @registry.command(
        "brain",
        "Inspeciona o second brain: árvore, uma pasta ou o conteúdo bruto de uma nota",
        aliases=("ver", "abrir", "inspecionar"),
        usage="[pasta | título ou caminho da nota]",
        examples=("mostra o brain", "o que tem no second brain"),
    )
    async def inspect(ctx: CommandContext) -> str | Reply:
        vault = ctx.services.vault
        if vault is None:
            return "O second brain não está configurado."
        ref = " ".join(ctx.args).strip()
        if ref in ("renomear", "renomeia"):
            return Reply.paginated(await rename_brain(ctx.services))
        if not ref:
            return Reply.paginated(await asyncio.to_thread(render_tree, vault))

        folder = vault.find_folder(ref)
        if folder is not None:
            return Reply.paginated(await asyncio.to_thread(render_listing, vault, folder, ctx.services.tz))

        try:
            entry, raw = await asyncio.to_thread(vault.inspect, ref)
        except NoteNotFoundError:
            return f"Não achei nota nem pasta '{ref}'. Veja a árvore com /brain."
        when = format_local(entry.modified, ctx.services.tz, "%d/%m/%Y %H:%M")
        header = f"📝 {entry.path} · {_size(entry)} · modificada {when}"
        if not raw.strip():
            return f"{header}\n(arquivo vazio)"
        return Reply.paginated(raw.strip(), header=header)


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
        vault.append_daily(f"Nota rápida: {saved.link}")
        await remember_ref(ctx.services, "nota", saved.path, f"nota: {saved.title}")
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


async def rename_brain(services) -> str:
    """/brain renomear: existing notes to the Portuguese snake_case pattern."""
    import asyncio

    from uva.brain.rename import apply_renames, plan_renames
    from uva.selfmod.git import SELF_EDIT

    vault, repo = services.vault, services.brain_repo
    if vault is None:
        return "O second brain não está configurado."
    renames = await plan_renames(services, vault)
    if not renames:
        return "Todos os nomes já estão no padrão (português, snake_case)."
    attachments = await asyncio.to_thread(apply_renames, vault, renames)
    lines = [f"✅ {len(renames)} nota(s) renomeada(s):"]
    lines += [f"- {r.old} → {r.new}" + (f" (título: {r.new_title})" if r.new_title != r.old_title else "") for r in renames]
    if attachments:
        lines += [f"{len(attachments)} anexo(s):"] + [f"- {a} → {b}" for a, b in attachments.items()]
    if repo is not None:
        try:
            await asyncio.to_thread(repo.run, "add", "-A")
            await asyncio.to_thread(
                repo.run, "commit", "-q", "-m",
                f"Renomeia {len(renames)} nota(s) para o padrão em português\n\n{SELF_EDIT}: true",
            )
            lines.append("Desfaz com /reverter brain.")
        except Exception as exc:
            lines.append(f"(não consegui commitar no git do brain: {exc})")
    return "\n".join(lines)
