"""The sandbox side of /sh and /py: a tiny HTTP server in the "shell" container.

Only standard library: it runs in its own container, which has no bot secrets
(no .env), no repository, no second brain and no projects folder, just the
/uva volume. The server runs as root only to start each command as the
unprivileged "uva" user (no sudo, no capabilities) inside /uva, and to keep
snapshots of /uva in /snapshots, which that user can't read or change.

When a command changes /uva, the state from before it is committed to a git
repository kept in /snapshots (the work tree is /uva), so /sh desfazer can
restore it.

API (JSON over HTTP, reachable only on the internal "shellnet" network):
  POST /run    {"kind": "sh"|"py", "code": "...", "label": "..."}
  POST /exists {"paths": [...]}
  POST /undo   {}
  GET  /history
"""

from __future__ import annotations

import json
import os

import signal
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock

HOME = Path(os.environ.get("UVA_HOME", "/uva"))
SNAPSHOTS = Path(os.environ.get("UVA_SNAPSHOTS", "/snapshots/repo.git"))
USER = os.environ.get("UVA_SHELL_USER", "uva")
TIMEOUT = int(os.environ.get("SHELL_TIMEOUT_SECONDS", "60"))
OUTPUT_LIMIT = int(os.environ.get("SHELL_OUTPUT_LIMIT", "60000"))
PORT = int(os.environ.get("SHELL_PORT", "8700"))
# Not worth snapshotting (big, rebuildable).
EXCLUDE = (".venv/", ".cache/", "__pycache__/", "node_modules/", ".npm/")

_lock = Lock()  # one command at a time: snapshots must follow the commands


def _account() -> tuple[int, int]:
    import pwd  # POSIX only (the module is also imported by the tests on Windows)

    entry = pwd.getpwnam(USER)
    return entry.pw_uid, entry.pw_gid


def _git(*args: str, check: bool = True, strip: bool = True) -> str:
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/root",
        "GIT_DIR": str(SNAPSHOTS), "GIT_WORK_TREE": str(HOME),
        "GIT_AUTHOR_NAME": "uva", "GIT_AUTHOR_EMAIL": "uva@localhost",
        "GIT_COMMITTER_NAME": "uva", "GIT_COMMITTER_EMAIL": "uva@localhost",
    }
    proc = subprocess.run(["git", *args], env=env, capture_output=True, text=True, cwd=HOME)
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {args[0]}: {proc.stderr.strip()[:300]}")
    return proc.stdout.strip() if strip else proc.stdout


def setup() -> None:
    uid, gid = _account()
    HOME.mkdir(parents=True, exist_ok=True)
    os.chown(HOME, uid, gid)
    SNAPSHOTS.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(SNAPSHOTS.parent, 0o700)  # root only
    if not (SNAPSHOTS / "HEAD").exists():
        subprocess.run(["git", "init", "-q", "--bare", str(SNAPSHOTS)], check=True)
        _git("config", "core.bare", "false")
    (SNAPSHOTS / "info").mkdir(exist_ok=True)
    (SNAPSHOTS / "info" / "exclude").write_text("\n".join(EXCLUDE) + "\n", encoding="utf-8")
    venv = HOME / ".venv"
    if not (venv / "bin" / "python").exists():
        _as_user(["python", "-m", "venv", str(venv)], timeout=120)


def _env() -> dict[str, str]:
    return {
        "HOME": str(HOME),
        "PATH": f"{HOME}/.venv/bin:{HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin",
        "VIRTUAL_ENV": f"{HOME}/.venv",
        "LANG": "C.UTF-8",
        "PYTHONUNBUFFERED": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "TERM": "dumb",
    }


def _as_user(argv: list[str], timeout: int = TIMEOUT, stdin: str | None = None) -> dict:
    uid, gid = _account()
    started = time.monotonic()
    proc = subprocess.Popen(
        argv, cwd=HOME, env=_env(), user=uid, group=gid, extra_groups=[],
        stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True,
    )
    timed_out = False
    try:
        out, _ = proc.communicate(stdin.encode() if stdin is not None else None, timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        os.killpg(proc.pid, signal.SIGKILL)
        out, _ = proc.communicate()
    text = out.decode("utf-8", errors="replace")
    truncated = len(text) > OUTPUT_LIMIT
    return {
        "exit": None if timed_out else proc.returncode,
        "timed_out": timed_out,
        "output": text[:OUTPUT_LIMIT],
        "truncated": truncated,
        "seconds": round(time.monotonic() - started, 2),
    }


def run(kind: str, code: str, label: str) -> dict:
    """Run a command; if it changed /uva, keep a snapshot of how it was before.

    The state before is staged in the index; only commands that changed
    something get a snapshot, so /sh desfazer undoes the last real change.
    """
    with _lock:
        _git("add", "-A")  # index = /uva before the command
        if kind == "py":
            result = _as_user(["python", "-"], stdin=code)
        else:
            result = _as_user(["bash", "-c", code])
        # Not stripped: " D a.txt" (deleted, not staged) starts with a space.
        changed = _git("status", "--porcelain", "--untracked-files=all", check=False, strip=False)
        unstaged = [line for line in changed.splitlines() if len(line) > 1 and line[1] != " "]
        result["snapshot"] = None
        if unstaged:
            _git("commit", "-q", "--allow-empty", "-m", f"antes: {(label or code)[:200]}")
            result["snapshot"] = _git("rev-parse", "--short", "HEAD")
        return result


def undo() -> dict:
    """Put /uva back as it was before the last command (and step back one)."""
    with _lock:
        log = _git("log", "--format=%h %s", "-2", check=False).splitlines()
        if not log:
            return {"ok": False, "error": "nada para desfazer"}
        target, label = log[0].split(" ", 1)
        changed = _git("status", "--porcelain", strip=False).splitlines()
        _git("reset", "-q", "--hard", target)
        _git("clean", "-q", "-fd")  # files the command created (excludes stay)
        uid, gid = _account()
        for path in [HOME, *HOME.rglob("*")]:
            try:
                if not any(part in EXCLUDE_NAMES for part in path.relative_to(HOME).parts):
                    os.lchown(path, uid, gid)
            except (OSError, ValueError):
                pass
        if len(log) > 1:
            _git("update-ref", "HEAD", "HEAD~1")  # next undo goes one step further back
        return {"ok": True, "restored": label.removeprefix("antes: "), "changed": changed[:50]}


EXCLUDE_NAMES = {e.strip("/") for e in EXCLUDE}


def history(limit: int = 10) -> list[str]:
    with _lock:
        return _git("log", f"-{limit}", "--format=%h %cd %s", "--date=format:%d/%m %H:%M", check=False).splitlines()


def exists(paths: list[str]) -> list[str]:
    found = []
    for raw in paths[:50]:
        path = (HOME / raw).resolve() if not raw.startswith("/") else Path(raw).resolve()
        if path.is_relative_to(HOME) and path.exists():
            found.append(raw)
    return found


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/history":
            self._send(200, {"history": history()})
        elif self.path == "/health":
            self._send(200, {"ok": True})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(length) or b"{}")
            if self.path == "/run":
                kind = data.get("kind", "sh")
                if kind not in ("sh", "py"):
                    raise ValueError("kind")
                self._send(200, run(kind, str(data.get("code", "")), str(data.get("label", ""))))
            elif self.path == "/undo":
                self._send(200, undo())
            elif self.path == "/exists":
                self._send(200, {"exists": exists([str(p) for p in data.get("paths", [])])})
            else:
                self._send(404, {"error": "not found"})
        except Exception as exc:  # report, never crash the server
            self._send(500, {"error": repr(exc)[:500]})

    def log_message(self, fmt: str, *args) -> None:
        print(json.dumps({"event": "shell_http", "line": fmt % args}), flush=True)


def main() -> None:
    setup()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(json.dumps({"event": "shell_ready", "port": PORT, "home": str(HOME)}), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
