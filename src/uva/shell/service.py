"""The bot side of /sh and /py: talks to the sandbox, asks before destroying."""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import httpx

from uva.clock import utcnow
from uva.config import Settings
from uva.i18n import t
from uva.logging.events import EventType, emit
from uva.progress import activity
from uva.replies import Button, Reply
from uva.safety import INVISIBLE
from uva.shell.guard import exfiltration_risks, floor_violations, python_risks, shell_risks

CONFIRM_TTL = timedelta(minutes=10)


class ShellUnavailable(RuntimeError):
    pass


@dataclass
class Pending:
    kind: str
    code: str
    reasons: list[str]
    expires_at: datetime = field(default_factory=lambda: utcnow() + CONFIRM_TTL)


class ShellService:
    def __init__(
        self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None, db=None
    ) -> None:
        self.settings = settings
        self.transport = transport
        self.pending: dict[str, Pending] = {}
        # SQLite (shell_allowlist): exact commands the user said to always allow.
        self.db = db

    def _client(self) -> httpx.AsyncClient:
        timeout = self.settings.shell_timeout_seconds + 30
        return httpx.AsyncClient(base_url=self.settings.shell_url, timeout=timeout, transport=self.transport)

    async def _call(self, method: str, path: str, payload: dict | None = None) -> dict:
        try:
            async with self._client() as client:
                response = await client.request(method, path, json=payload)
        except httpx.HTTPError as exc:
            raise ShellUnavailable(t("shell.service.unavailable", error=exc.__class__.__name__)) from exc
        data = response.json()
        if response.status_code != 200:
            raise ShellUnavailable(data.get("error", f"HTTP {response.status_code}"))
        return data

    def refusal(self, code: str) -> str | None:
        """Why ``code`` is never run (even confirmed), or None."""
        if INVISIBLE.search(code):
            return t("shell.service.refused_invisible")
        floor = floor_violations(code)
        if floor:
            return t("shell.service.refused_floor", what=", ".join(t(f"shell.guard.floor.{f}") for f in floor))
        return None

    async def risks(self, kind: str, code: str) -> list[str]:
        if kind == "py":
            return python_risks(code) + exfiltration_risks(code)
        reasons, maybe = shell_risks(code)
        reasons += exfiltration_risks(code)
        if maybe:
            existing = (await self._call("POST", "/exists", {"paths": maybe}))["exists"]
            reasons += [t("shell.service.overwrites", path=p) for p in existing]
        return reasons

    async def request(self, kind: str, code: str) -> Reply:
        """Run now, or ask first when it may delete/overwrite files."""
        code = code.strip()
        if not code:
            return Reply(usage())
        if not self.settings.shell_enabled:
            return Reply(t("shell.service.disabled"))
        if (why := self.refusal(code)) is not None:
            await emit(EventType.SHELL, op="refused", kind=kind, code=code[:500], reason=why)
            return Reply(why)
        if await self.is_allowed(kind, code):
            return await self.execute(kind, code, confirmed=["allowlist"])
        try:
            reasons = await self.risks(kind, code)
        except ShellUnavailable as exc:
            return Reply(f"⚠️ {exc}")
        if reasons:
            return self._ask(kind, code, reasons)
        return await self.execute(kind, code)

    def _ask(self, kind: str, code: str, reasons: list[str]) -> Reply:
        now = utcnow()
        for token in [t for t, p in self.pending.items() if p.expires_at < now]:
            del self.pending[token]
        token = secrets.token_hex(4)
        self.pending[token] = Pending(kind, code, reasons)
        shown = code if len(code) <= 600 else code[:600] + "…"
        return Reply(
            text=t(
                "shell.service.confirm", reasons="; ".join(reasons), prompt="🐍" if kind == "py" else "$",
                code=shown,
            ),
            buttons=[
                Button(t("shell.service.yes"), f"shok:{token}"),
                *([Button(t("shell.service.always"), f"shal:{token}")] if self.db is not None else []),
                Button(t("shell.service.no"), f"shno:{token}"),
            ],
        )

    async def resolve(self, verb: str, token: str) -> Reply:
        pending = self.pending.pop(token, None)
        if pending is None or pending.expires_at < utcnow():
            return Reply(t("shell.service.expired"))
        if verb not in ("shok", "shal"):
            await emit(EventType.SHELL, op="denied", kind=pending.kind, code=pending.code[:500])
            return Reply(t("shell.service.denied"))
        if (why := self.refusal(pending.code)) is not None:  # the floor holds even when confirmed
            return Reply(why)
        if verb == "shal":
            await self.allow(pending.kind, pending.code)
        return await self.execute(pending.kind, pending.code, confirmed=pending.reasons)

    # --- allowlist ("Sempre permitir": the exact command, nothing similar) -------------

    async def is_allowed(self, kind: str, code: str) -> bool:
        if self.db is None:
            return False
        try:
            row = await self.db.run(lambda conn: conn.execute(
                "SELECT 1 FROM shell_allowlist WHERE kind = ? AND code = ?", (kind, code)
            ).fetchone())
        except Exception:
            return False
        return row is not None

    async def allow(self, kind: str, code: str) -> None:
        await self.db.run(lambda conn: conn.execute(
            "INSERT OR IGNORE INTO shell_allowlist (kind, code, created_at) VALUES (?, ?, ?)",
            (kind, code, utcnow().isoformat()),
        ))
        await emit(EventType.SHELL, op="allowlisted", kind=kind, code=code[:500])

    async def allowlist(self) -> list[tuple[int, str, str]]:
        """(id, kind, code), oldest first."""
        if self.db is None:
            return []
        rows = await self.db.run(lambda conn: conn.execute(
            "SELECT id, kind, code FROM shell_allowlist ORDER BY id"
        ).fetchall())
        return [(r["id"], r["kind"], r["code"]) for r in rows]

    async def disallow(self, entry_id: int) -> bool:
        if self.db is None:
            return False
        count = await self.db.run(lambda conn: conn.execute(
            "DELETE FROM shell_allowlist WHERE id = ?", (entry_id,)
        ).rowcount)
        return count > 0

    async def execute(self, kind: str, code: str, confirmed: list[str] | None = None) -> Reply:
        await activity("executando")
        try:
            result = await self.run(code, kind, confirmed=confirmed)
        except ShellUnavailable as exc:
            return Reply(f"⚠️ {exc}")
        return render_result(kind, code, result, self.settings.shell_timeout_seconds)

    async def run(self, code: str, kind: str = "sh", confirmed: list[str] | None = None) -> dict:
        """Run in the sandbox and return its raw result (output, exit, timed_out...).
        Raises ShellUnavailable. No confirmation here: callers check risks()."""
        result = await self._call("POST", "/run", {"kind": kind, "code": code, "label": code[:200]})
        await emit(
            EventType.SHELL, op="run", kind=kind, code=code[:2000], exit=result.get("exit"),
            timed_out=result.get("timed_out"), seconds=result.get("seconds"),
            snapshot=result.get("snapshot"), confirmed=confirmed, output=result.get("output", "")[:2000],
        )
        return result

    async def undo(self) -> Reply:
        try:
            result = await self._call("POST", "/undo", {})
        except ShellUnavailable as exc:
            return Reply(f"⚠️ {exc}")
        await emit(EventType.SHELL, op="undo", result=result)
        if not result.get("ok"):
            error = result.get("error")
            if error in EMPTY_UNDO:  # the sandbox answers in English (stdlib only, no i18n)
                error = t("shell.service.undo_empty")
            return Reply(t("shell.service.nothing_to_undo", error=error))
        changed = result.get("changed") or []
        lines = [t("shell.service.restored", label=result["restored"])]
        if changed:
            lines.append(t("shell.service.undone") + "\n" + "\n".join(f"  {c}" for c in changed[:30]))
        return Reply("\n".join(lines))

    async def history(self) -> Reply:
        try:
            result = await self._call("GET", "/history")
        except ShellUnavailable as exc:
            return Reply(f"⚠️ {exc}")
        lines = result.get("history") or []
        if not lines:
            return Reply(t("shell.service.no_history"))
        return Reply.paginated(t("shell.service.history") + "\n" + "\n".join(lines))


# The "nothing to undo" error from the sandbox (shell/executor.py NOTHING_TO_UNDO),
# plus the Portuguese one an executor image built before the change still sends.
EMPTY_UNDO = ("nothing to undo", "nada para desfazer")


def usage() -> str:
    return t("shell.service.usage")


# Kept for importers that still read the constant (Portuguese); prefer usage().
USAGE = t("shell.service.usage", lang="pt")


def render_result(kind: str, code: str, result: dict, timeout: int) -> Reply:
    prompt = "🐍" if kind == "py" else "$"
    head = f"{prompt} {code if len(code) <= 200 else code[:200] + '…'}"
    output = (result.get("output") or "").rstrip() or t("shell.service.no_output")
    if result.get("truncated"):
        output += "\n" + t("shell.service.truncated")
    if result.get("timed_out"):
        status = t("shell.service.timed_out", seconds=timeout)
    elif result.get("exit") == 0:
        status = "✅ ok"
    else:
        status = t("shell.service.exit_code", code=result.get("exit"))
    status += f" · {result.get('seconds', 0)}s"
    if result.get("snapshot"):
        status += t("shell.service.changed_files", snapshot=result["snapshot"])
    reply = Reply.paginated(f"{output}\n\n{status}", header=head)
    reply.summary = f"{head} → {status}"
    return reply
