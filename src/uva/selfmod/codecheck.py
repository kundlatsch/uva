"""Static checks for self-written plugins, before any of their code runs.

An allowlist, not a denylist: plugins may only import harmless standard
modules (and httpx for public web APIs), may not call the builtins that
reach files, code execution or introspection, and may not touch private
attributes (``obj._x``, ``obj.__class__``...). That rules out subprocess,
os.system, reading .env, importing config/security, etc. The checker is a
guard rail against mistakes and prompt injection, not a full sandbox.
"""

from __future__ import annotations

import ast
import re

PLUGIN_IMPORTS = {
    "__future__",
    "uva.plugins.api",
    "random",
    "math",
    "statistics",
    "datetime",
    "zoneinfo",
    "re",
    "json",
    "string",
    "itertools",
    "functools",
    "collections",
    "dataclasses",
    "typing",
    "enum",
    "decimal",
    "fractions",
    "textwrap",
    "unicodedata",
    "calendar",
    "httpx",
}
TEST_EXTRA_IMPORTS = {"pytest", "uva.plugins.testing"}

FORBIDDEN_NAMES = {
    "open",
    "eval",
    "exec",
    "compile",
    "__import__",
    "globals",
    "locals",
    "vars",
    "getattr",
    "setattr",
    "delattr",
    "breakpoint",
    "input",
    "exit",
    "quit",
    "memoryview",
    "help",
}

NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,30}$")
MAX_CODE_CHARS = 20_000


def check_plugin(code: str) -> list[str]:
    return _check(code, PLUGIN_IMPORTS)


def check_test(code: str, plugin_name: str) -> list[str]:
    allowed = PLUGIN_IMPORTS | TEST_EXTRA_IMPORTS | {f"uva.plugins.{plugin_name}"}
    return _check(code, allowed)


def check_name(name: str, taken: set[str]) -> list[str]:
    if not NAME_PATTERN.match(name):
        return [f"invalid module name: {name!r} (use a-z, 0-9 and _)"]
    if name in taken:
        return [f"a plugin/module named {name!r} already exists"]
    return []


def _check(code: str, allowed_imports: set[str]) -> list[str]:
    if len(code) > MAX_CODE_CHARS:
        return [f"code too long ({len(code)} characters; limit {MAX_CODE_CHARS})"]
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return [f"syntax error: {exc}"]

    problems: list[str] = []
    for node in ast.walk(tree):
        line = getattr(node, "lineno", "?")
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name not in allowed_imports:
                    problems.append(f"line {line}: forbidden import: {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                problems.append(f"line {line}: relative imports are not allowed")
            elif node.module not in allowed_imports:
                problems.append(f"line {line}: forbidden import: {node.module}")
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            problems.append(f"line {line}: forbidden use of {node.id}()")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            problems.append(f"line {line}: forbidden private/magic attribute: .{node.attr}")
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            problems.append(f"line {line}: global/nonlocal is not allowed")
    return problems
