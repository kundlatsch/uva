"""Discovering and (re)loading plugin commands from the plugins folder.

Plugins are loaded by file path (not as package imports) so that a running
bot picks up a plugin merged into the mounted repository without rebuilding
the image. Reloading removes every plugin command and loads the folder again.
"""

from __future__ import annotations

import importlib.util
import inspect
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

from uva.clock import utcnow
from uva import i18n
from uva.commands.registry import Command, CommandContext, CommandRegistry
from uva.logging import get_logger
from uva.plugins.api import PluginCommand, PluginContext, commands_in, resolve_description
from uva.selfmod.plugin_data import make_data_view

log = get_logger(__name__)

# Modules in the plugins folder that are infrastructure, not plugins.
RESERVED = {"__init__", "api", "testing"}
MODULE_PREFIX = "uva_plugin_"


@dataclass(slots=True)
class LoadReport:
    loaded: dict[str, list[str]] = field(default_factory=dict)  # file stem -> command names
    errors: dict[str, str] = field(default_factory=dict)  # file stem -> error

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict:
        return {"loaded": self.loaded, "errors": self.errors}


def default_plugins_dir() -> Path:
    import uva.plugins

    return Path(uva.plugins.__file__).parent


def plugin_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(
        p for p in directory.glob("*.py") if p.stem not in RESERVED and not p.stem.startswith("_")
    )


def load_module(path: Path) -> ModuleType:
    name = MODULE_PREFIX + path.stem
    sys.modules.pop(name, None)  # always load the current file contents
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def load_plugins(registry: CommandRegistry, directory: Path) -> LoadReport:
    """Replace all plugin commands in ``registry`` with the folder's current ones."""
    registry.remove_source("plugin")
    report = LoadReport()
    for path in plugin_files(directory):
        try:
            found = commands_in(load_module(path))
            if not found:
                raise ValueError("no command with @command")
            _register_all(registry, found)
        except Exception as exc:
            report.errors[path.stem] = f"{type(exc).__name__}: {exc}"
            log.warning("plugin_load_failed", plugin=path.stem, error=str(exc))
            continue
        report.loaded[path.stem] = [c.name for c in found]
    return report


def _register_all(registry: CommandRegistry, found: list[PluginCommand]) -> None:
    """All of a module's commands or none of them."""
    commands = [_to_command(pc) for pc in found]
    taken = [n for c in commands for n in c.all_names if registry.get(n) is not None]
    if taken:
        raise ValueError(f"name already used by another command: {', '.join(taken)}")
    for cmd in commands:
        registry.register(cmd)


def _to_command(pc: PluginCommand) -> Command:
    async def handler(ctx: CommandContext) -> str:
        now = utcnow().astimezone(ctx.services.tz)
        data = make_data_view(ctx.services)
        context = PluginContext(
            args=list(ctx.args), now=now, data=data, lang=i18n.get_language()
        )
        result = pc.handler(context)
        if inspect.isawaitable(result):
            result = await result
        return str(result)

    return Command(
        name=pc.name,
        # Resolved once, when (re)loading: the language is set at startup.
        description=resolve_description(pc.description, i18n.get_language()),
        handler=handler,
        aliases=tuple(pc.aliases),
        usage=resolve_description(pc.usage, i18n.get_language()),
        examples=tuple(pc.examples),
        source="plugin",
        placeholder=pc.placeholder,
    )
