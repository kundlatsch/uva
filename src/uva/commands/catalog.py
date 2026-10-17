"""/comandos: every command and every LLM action, for debugging."""

from __future__ import annotations

from uva.commands.registry import CommandContext, CommandRegistry
from uva.i18n import t
from uva.replies import Reply
from uva.services import Services


def render_catalog(services: Services) -> str:
    lines = [t("commands.catalog.commands_header"), ""]
    for cmd in sorted(services.registry, key=lambda c: (c.source != "core", c.name)):
        head = f"/{cmd.name}" + (f" {cmd.usage}" if cmd.usage else "")
        lines.append(f"- {head} [{cmd.source}]: {cmd.description}")
        if cmd.aliases:
            lines.append(t("commands.catalog.aliases", aliases=", ".join(cmd.aliases)))
        if cmd.examples:
            lines.append(t("commands.catalog.examples", examples="; ".join(cmd.examples)))

    lines += ["", t("commands.catalog.actions_header"), ""]
    flag_names = (
        (t("commands.catalog.flag_proactive"), "proactive_safe"),
        (t("commands.catalog.flag_task"), "long_running"),
    )
    for action in services.actions or []:
        flags = [f for f, attr in flag_names if getattr(action, attr)]
        suffix = f" [{', '.join(flags)}]" if flags else ""
        lines.append(f"- {action.name}{suffix}: {action.description}")
        lines.append(f"  params: {describe_params(action.params_model)}")
    return "\n".join(lines)


def describe_params(model) -> str:
    schema = model.model_json_schema()
    required = set(schema.get("required", []))
    parts = []
    for name, prop in schema.get("properties", {}).items():
        kind = prop.get("type") or "|".join(
            p.get("type", "?") for p in prop.get("anyOf", []) if p.get("type") != "null"
        ) or ("enum" if "enum" in prop or "$ref" in prop else "?")
        parts.append(f"{name}: {kind}" + ("" if name in required else "?"))
    return ", ".join(parts) or t("commands.catalog.no_params")


def register(registry: CommandRegistry) -> None:
    @registry.command(
        "comandos",
        t("commands.catalog.comandos.description"),
        aliases=("catalogo", "catálogo", "commands", "catalog"),
        examples=("lista de comandos", "todos os comandos"),
    )
    async def catalog(ctx: CommandContext) -> Reply:
        text = render_catalog(ctx.services)
        return Reply(text=text, filename=t("commands.catalog.comandos.filename")).fit_for_telegram()
