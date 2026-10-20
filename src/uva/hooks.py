"""Local webhooks: ``POST /hook/<name>`` on the dashboard (DASHBOARD_HOOK_TOKEN).

``data/hooks.json`` maps a name to what the call does:

    {"backup": {"notice": "💾 Backup feito: {body}"},
     "nas": {"request": "vê se o disco do NAS está acabando e me avisa só se estiver", "skill": ""}}

A ``notice`` is sent as is (``{body}`` is the call's body). A ``request`` runs
through the router like a routine (a proactive run: no memory writes), with
the body as data in its context, and its reply is sent. The dashboard only
queues the call (Celery ``uva.hook``); the worker runs it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from uva.i18n import t
from uva.safety import as_data, sanitize_untrusted

NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
MAX_BODY = 20000


class HookError(ValueError):
    pass


def hooks_file(settings) -> Path:
    return settings.repo_dir / "data" / "hooks.json"


def load(settings) -> dict[str, dict]:
    path = hooks_file(settings)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except ValueError as exc:
        raise HookError(t("hooks.bad_file", error=exc)) from exc
    return {k: v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}


def find(settings, name: str) -> dict:
    if not NAME.match(name):
        raise HookError(t("hooks.unknown", name=name[:40]))
    entry = load(settings).get(name)
    if entry is None or not (entry.get("notice") or entry.get("request")):
        raise HookError(t("hooks.unknown", name=name))
    return entry


async def run_hook(services, name: str, body: str, notify) -> str:
    """Do what hooks.json says for ``name``; returns "notice" | "request" | "silent"."""
    from uva.replies import Reply

    entry = find(services.settings, name)
    body = body[:MAX_BODY]
    if entry.get("notice"):
        await notify(Reply(str(entry["notice"]).replace("{body}", body.strip()), kind="hook", ref=name))
        return "notice"
    from uva.provenance import incoming, system_turn
    from uva.router import Router
    from uva.routines import classify

    clean, _ = sanitize_untrusted(body)
    note = (f"This request comes from the local webhook {name!r} (the user set it up; he did not "
            "ask right now). Report only what is useful; answer [SILENT] if there is nothing to tell.\n"
            + as_data(f"webhook {name} payload", clean or "(empty)"))
    with system_turn(), incoming("webhook"):
        reply = await Router(services).handle_text(str(entry["request"]), skill=entry.get("skill") or None, note=note)
    status, text = classify(reply.text)
    if status == "silent":
        return "silent"
    await notify(Reply(t("hooks.head", name=name) + "\n" + text, kind="hook", ref=name, buttons=reply.buttons))
    return "request"
