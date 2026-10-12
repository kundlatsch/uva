"""Spot commands that delete or overwrite files, so they need a "yes" first.

It is a heuristic (a shell can delete in endless ways), so every command also
runs after a snapshot of /uva that /sh desfazer restores.
"""

from __future__ import annotations

import re
import shlex

# Always destructive.
DELETERS = frozenset({"rm", "rmdir", "unlink", "shred", "truncate", "dd", "srm", "wipe"})
# Destructive when the target exists (checked in the sandbox).
OVERWRITERS = frozenset({"mv", "cp", "install", "ln", "rsync", "tee"})
SEPARATORS = re.compile(r"\|\||&&|[;|&\n]")
REDIRECT = re.compile(r"(?<![<>&\d])>(?!>|&)\s*([^\s;|&<>]+)")  # "> file", not ">>" nor "2>&1"
PYTHON_C = re.compile(r"""\bpython3?\s+-c\s+(['"])(.*?)\1""", re.DOTALL)
SH_PATTERNS = (
    (re.compile(r"\bfind\b.*\s-(delete|exec\s+rm)\b"), "find -delete"),
    (re.compile(r"\bgit\s+(clean|reset\s+--hard|checkout\s+--|restore)\b"), "git apagando mudanças"),
    (re.compile(r"\bsed\b[^|;&]*\s-i"), "sed -i (edita no lugar)"),
    (re.compile(r"\bxargs\b[^|;&]*\brm\b"), "xargs rm"),
)
PY_PATTERNS = (
    (re.compile(r"\b(os|shutil|pathlib|Path)\b.*\b(remove|unlink|rmdir|rmtree|removedirs|truncate)\b"), "apaga arquivos"),
    (re.compile(r"\.(unlink|rmdir|rmtree)\s*\("), "apaga arquivos"),
    (re.compile(r"\bos\.(rename|replace)\s*\("), "renomeia/sobrescreve"),
    (re.compile(r"\bshutil\.(move|copy\w*)\s*\("), "move/copia (pode sobrescrever)"),
    (re.compile(r"""open\s*\([^)]*['"][wx]b?\+?['"]"""), "abre arquivo para escrita (sobrescreve)"),
    (re.compile(r"\.write_(text|bytes)\s*\("), "escreve arquivo (sobrescreve)"),
    (re.compile(r"\bsubprocess\b|\bos\.system\b"), "roda comandos do sistema"),
)


def _words(segment: str) -> list[str]:
    try:
        return shlex.split(segment, comments=True)
    except ValueError:
        return segment.split()


def shell_risks(command: str) -> tuple[list[str], list[str]]:
    """(reasons it is destructive for sure, paths that make it destructive if they exist)."""
    reasons: list[str] = []
    maybe: list[str] = []
    for label_re, label in SH_PATTERNS:
        if label_re.search(command):
            reasons.append(label)
    for _, code in PYTHON_C.findall(command):  # before splitting: the code may contain ";"
        reasons.extend(python_risks(code))
    for target in REDIRECT.findall(command):
        if target not in ("/dev/null", "/dev/stdout", "/dev/stderr"):
            maybe.append(target)
    for segment in SEPARATORS.split(command):
        words = [w for w in _words(segment) if w not in ("sudo", "command", "builtin", "env", "nohup", "time")]
        if not words:
            continue
        name = words[0].rsplit("/", 1)[-1]
        args = [w for w in words[1:] if not w.startswith("-")]
        if name in DELETERS:
            reasons.append(f"{name} {' '.join(args)}".strip())
        elif name in OVERWRITERS and args:
            if name == "tee":
                maybe.extend(args)
            else:
                maybe.append(args[-1])  # the destination
                if name == "mv":
                    reasons.append(f"mv {' '.join(args)}")  # moving removes the source
    return _unique(reasons), _unique(maybe)


def python_risks(code: str) -> list[str]:
    return _unique([label for pattern, label in PY_PATTERNS if pattern.search(code)])


def _unique(items: list[str]) -> list[str]:
    out: list[str] = []
    for item in items:
        if item not in out:
            out.append(item)
    return out
