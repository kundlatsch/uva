"""Running the test suite of a working copy in a subprocess.

The subprocess gets a minimal environment (no tokens or API keys), so even a
plugin test that slipped past the static check can't read the bot's secrets
from the environment.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# Only what Python/pytest need to start, on Linux and on Windows.
_PASSTHROUGH = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR", "LANG", "TZ")
OUTPUT_TAIL = 6000


@dataclass(frozen=True, slots=True)
class SuiteResult:
    ok: bool
    summary: str  # last pytest line, e.g. "165 passed in 8.1s"
    output: str  # tail of the output, fed back to the LLM on failure


def run_pytest(workdir: Path, timeout: int = 300) -> SuiteResult:
    env = {k: os.environ[k] for k in _PASSTHROUGH if k in os.environ}
    env.update(
        {
            "PYTHONPATH": str(workdir / "src"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "HOME": str(workdir),
            "PYTHONIOENCODING": "utf-8",
        }
    )
    command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--maxfail=20"]
    try:
        result = subprocess.run(
            command,
            cwd=workdir,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return SuiteResult(False, f"timeout ({timeout}s)", f"Os testes passaram de {timeout}s.")
    output = (result.stdout + result.stderr).strip()
    return SuiteResult(result.returncode == 0, _summary(output), output[-OUTPUT_TAIL:])


def _summary(output: str) -> str:
    for line in reversed(output.splitlines()):
        if re.search(r"\d+ (passed|failed|error)", line):
            return line.strip("= ").strip()
    return output.splitlines()[-1] if output else "sem saída"
