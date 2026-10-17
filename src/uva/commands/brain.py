"""/nota, /buscar and /brain: quick access to the second brain."""

from __future__ import annotations

import asyncio
from pathlib import Path

from uva.brain.vault import Entry, NoteNotFoundError, Vault, title_from_text
from uva.clock import format_local
from uva.commands.registry import CommandContext, CommandRegistry
from uva.i18n import t
from uva.replies import Reply
from uva.stm.refs import remember_ref

SEARCH_LIMIT = 10
TREE_FILES_PER_FOLDER = 30  # the tree is paginated, so show plenty


def _size(entry: Entry) -> str:
    if entry.is_dir:
        return t("commands.brain.notes_count", count=entry.size)
    return f"{entry.size} B" if entry.size < 1024 else f"{entry.size / 1024:.1f} KB"


def render_listing(vault: Vault, folder: Path, tz) -> str:
    entries = vault.list_folder(folder)
    where = vault._relative(folder) if folder != vault.root.resolve() else "brain"
    if not entries:
        return t("commands.brain.empty_folder", where=where)
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
        lines.append(f"📁 {entry.path}/ ({_size(entry)})")
        notes = sorted(
            (p for p in (vault.root / entry.path).rglob("*.md") if vault._visible(p)),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for path in notes[:TREE_FILES_PER_FOLDER]:
            lines.append(f"   · {vault._relative(path).split('/', 1)[1]}")
        if len(notes) > TREE_FILES_PER_FOLDER:
            lines.append(
                t("commands.brain.more", count=len(notes) - TREE_FILES_PER_FOLDER, path=entry.path)
            )
    return "\n".join(lines)


def register(registry: CommandRegistry) -> None:
    @registry.command(
        "brain",
        t("commands.brain.brain.description"),
        aliases=("cerebro", "cérebro", "ver", "abrir", "inspecionar", "view", "open", "inspect"),
        usage=t("commands.brain.brain.usage"),
        examples=("mostra o brain", "o que tem no second brain"),
    )
    async def inspect(ctx: CommandContext) -> str | Reply:
        vault = ctx.services.vault
        if vault is None:
            return t("commands.brain.not_configured")
        ref = " ".join(ctx.args).strip()
        if ref in ("renomear", "renomeia", "rename"):
            return Reply.paginated(await rename_brain(ctx.services))
        if not ref:
            return Reply.paginated(await asyncio.to_thread(render_tree, vault))

        folder = vault.find_folder(ref)
        if folder is not None:
            return Reply.paginated(await asyncio.to_thread(render_listing, vault, folder, ctx.services.tz))

        try:
            entry, raw = await asyncio.to_thread(vault.inspect, ref)
        except NoteNotFoundError:
            return t("commands.brain.brain.not_found", ref=ref)
        when = format_local(entry.modified, ctx.services.tz, "%d/%m/%Y %H:%M")
        header = t("commands.brain.brain.header", path=entry.path, size=_size(entry), when=when)
        if not raw.strip():
            return f"{header}\n{t('commands.brain.brain.empty_file')}"
        return Reply.paginated(raw.strip(), header=header)


    @registry.command(
        "nota",
        t("commands.brain.nota.description"),
        aliases=("anota", "anotar", "note"),
        usage=t("commands.brain.nota.usage_arg"),
    )
    async def note(ctx: CommandContext) -> str:
        vault = ctx.services.vault
        if vault is None:
            return t("commands.brain.not_configured")
        text = " ".join(ctx.args).strip()
        if not text:
            return t("commands.brain.nota.usage")
        saved = vault.create_note(title_from_text(text), text, folder="inbox", tags=["inbox"])
        vault.append_daily(t("commands.brain.nota.daily", link=saved.link))
        await remember_ref(ctx.services, "nota", saved.path, f"note: {saved.title}")
        return t("commands.brain.nota.saved", path=saved.path)

    @registry.command(
        "buscar",
        t("commands.brain.buscar.description"),
        aliases=("procurar", "search"),
        usage=t("commands.brain.buscar.usage_arg"),
        examples=("procura nas notas",),
    )
    async def search(ctx: CommandContext) -> str:
        vault = ctx.services.vault
        if vault is None:
            return t("commands.brain.not_configured")
        term = " ".join(ctx.args).strip()
        if not term:
            return t("commands.brain.buscar.usage")
        hits = await asyncio.to_thread(vault.search, term, SEARCH_LIMIT)
        if not hits:
            no_results = t("commands.brain.buscar.no_results", term=term)
            return f"{ctx.services.say('sem_resultado', termo=term)}\n{no_results}"
        return t("commands.brain.buscar.results", term=term) + "\n" + "\n".join(
            f"- {h.path}\n  {h.snippet}" for h in hits
        )


async def rename_brain(services) -> str:
    """/brain renomear: existing notes to the Portuguese snake_case pattern."""
    import asyncio

    from uva.brain.rename import apply_renames, plan_renames
    from uva.selfmod.git import SELF_EDIT

    vault, repo = services.vault, services.brain_repo
    if vault is None:
        return t("commands.brain.not_configured")
    renames = await plan_renames(services, vault)
    if not renames:
        return t("commands.brain.rename.nothing")
    attachments = await asyncio.to_thread(apply_renames, vault, renames)
    lines = [t("commands.brain.rename.renamed", count=len(renames))]
    lines += [
        f"- {r.old} → {r.new}"
        + (t("commands.brain.rename.new_title", title=r.new_title) if r.new_title != r.old_title else "")
        for r in renames
    ]
    if attachments:
        lines += [t("commands.brain.rename.attachments", count=len(attachments))]
        lines += [f"- {a} → {b}" for a, b in attachments.items()]
    if repo is not None:
        try:
            await asyncio.to_thread(repo.run, "add", "-A")
            await asyncio.to_thread(
                repo.run, "commit", "-q", "-m",
                f"Rename {len(renames)} note(s) to the Portuguese naming pattern\n\n{SELF_EDIT}: true",
            )
            lines.append(t("commands.brain.rename.undo"))
        except Exception as exc:
            lines.append(t("commands.brain.rename.commit_failed", error=exc))
    return "\n".join(lines)
