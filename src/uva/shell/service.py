"""The bot side of /sh and /py: talks to the sandbox, asks before destroying."""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import httpx

from uva.clock import utcnow
from uva.config import Settings
from uva.logging.events import EventType, emit
from uva.replies import Button, Reply
from uva.shell.guard import python_risks, shell_risks

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
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.settings = settings
        self.transport = transport
        self.pending: dict[str, Pending] = {}

    def _client(self) -> httpx.AsyncClient:
        timeout = self.settings.shell_timeout_seconds + 30
        return httpx.AsyncClient(base_url=self.settings.shell_url, timeout=timeout, transport=self.transport)

    async def _call(self, method: str, path: str, payload: dict | None = None) -> dict:
        try:
            async with self._client() as client:
                response = await client.request(method, path, json=payload)
        except httpx.HTTPError as exc:
            raise ShellUnavailable(f"o container shell não respondeu ({exc.__class__.__name__})") from exc
        data = response.json()
        if response.status_code != 200:
            raise ShellUnavailable(data.get("error", f"HTTP {response.status_code}"))
        return data

    async def risks(self, kind: str, code: str) -> list[str]:
        if kind == "py":
            return python_risks(code)
        reasons, maybe = shell_risks(code)
        if maybe:
            existing = (await self._call("POST", "/exists", {"paths": maybe}))["exists"]
            reasons += [f"sobrescreve {p}" for p in existing]
        return reasons

    async def request(self, kind: str, code: str) -> Reply:
        """Run now, or ask first when it may delete/overwrite files."""
        code = code.strip()
        if not code:
            return Reply(USAGE)
        if not self.settings.shell_enabled:
            return Reply("O /uva está desligado (SHELL_ENABLED=false).")
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
            text=(
                f"⚠️ Isso pode apagar ou sobrescrever arquivos em /uva ({'; '.join(reasons)}):\n"
                f"{'🐍' if kind == 'py' else '$'} {shown}\n\nRodo mesmo? (antes eu tiro um snapshot; "
                "dá para voltar com /sh desfazer)"
            ),
            buttons=[Button("Sim, rodar", f"shok:{token}"), Button("Não", f"shno:{token}")],
        )

    async def resolve(self, verb: str, token: str) -> Reply:
        pending = self.pending.pop(token, None)
        if pending is None or pending.expires_at < utcnow():
            return Reply("Esse pedido expirou; mande o comando de novo.")
        if verb != "shok":
            await emit(EventType.SHELL, op="denied", kind=pending.kind, code=pending.code[:500])
            return Reply("Ok, não rodei nada.")
        return await self.execute(pending.kind, pending.code, confirmed=pending.reasons)

    async def execute(self, kind: str, code: str, confirmed: list[str] | None = None) -> Reply:
        try:
            result = await self._call("POST", "/run", {"kind": kind, "code": code, "label": code[:200]})
        except ShellUnavailable as exc:
            return Reply(f"⚠️ {exc}")
        await emit(
            EventType.SHELL, op="run", kind=kind, code=code[:2000], exit=result.get("exit"),
            timed_out=result.get("timed_out"), seconds=result.get("seconds"),
            snapshot=result.get("snapshot"), confirmed=confirmed, output=result.get("output", "")[:2000],
        )
        return render_result(kind, code, result, self.settings.shell_timeout_seconds)

    async def undo(self) -> Reply:
        try:
            result = await self._call("POST", "/undo", {})
        except ShellUnavailable as exc:
            return Reply(f"⚠️ {exc}")
        await emit(EventType.SHELL, op="undo", result=result)
        if not result.get("ok"):
            return Reply(f"Nada para desfazer ({result.get('error')}).")
        changed = result.get("changed") or []
        lines = [f"↩️ /uva voltou para antes de: {result['restored']}"]
        if changed:
            lines.append("Desfeito:\n" + "\n".join(f"  {c}" for c in changed[:30]))
        return Reply("\n".join(lines))

    async def history(self) -> Reply:
        try:
            result = await self._call("GET", "/history")
        except ShellUnavailable as exc:
            return Reply(f"⚠️ {exc}")
        lines = result.get("history") or []
        if not lines:
            return Reply("Nenhum comando ainda.")
        return Reply.paginated("Snapshots de /uva (o mais novo primeiro):\n" + "\n".join(lines))


USAGE = (
    "Uso:\n/sh <comando> — roda um comando Linux em /uva\n/py <código> — roda Python em /uva\n"
    "/sh desfazer — volta /uva para antes do último comando\n/sh historico — snapshots"
)


def render_result(kind: str, code: str, result: dict, timeout: int) -> Reply:
    prompt = "🐍" if kind == "py" else "$"
    head = f"{prompt} {code if len(code) <= 200 else code[:200] + '…'}"
    output = (result.get("output") or "").rstrip() or "(sem saída)"
    if result.get("truncated"):
        output += "\n… (saída cortada)"
    if result.get("timed_out"):
        status = f"⏱️ parado depois de {timeout}s (SHELL_TIMEOUT_SECONDS)"
    else:
        status = "✅ ok" if result.get("exit") == 0 else f"❌ saiu com código {result.get('exit')}"
    status += f" · {result.get('seconds', 0)}s"
    if result.get("snapshot"):
        status += f" · mudou arquivos (snapshot {result['snapshot']}; /sh desfazer)"
    reply = Reply.paginated(f"{output}\n\n{status}", header=head)
    reply.summary = f"{head} → {status}"
    return reply
