"""Local dashboard: overview, costs, settings, logs and a full chat with debug.

Run by the "dashboard" service (published only on 127.0.0.1). The chat goes
through the same Router as Telegram: commands, similarity, LLM, actions and
the shared short-term memory, so both channels see the same conversation.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import secrets
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, SecretStr

from uva.bootstrap import build_services, setup_storage
from uva.clock import format_local, start_of_day, start_of_month, utcnow
from uva.config import Settings, get_settings
from uva.i18n import DEFAULT, catalog, get_language, t
from uva.index.service import turn_source
from uva.llm.client import debug_calls
from uva.logging import events
from uva.logging.events import Event, EventQuery, EventType
from uva.progress import LiveSink, with_feedback
from uva.replies import Reply
from uva.router import Router
from uva.storage import mongo

STATIC = Path(__file__).parent / "static"
SECRET_NAME = re.compile(r"key|token|password|secret", re.IGNORECASE)
URI_PASSWORD = re.compile(r"(?P<scheme>[a-z+]+://)(?P<user>[^:/@]+):(?P<password>[^@]+)@")
STARTED = time.time()

_captured: ContextVar[list[Event] | None] = ContextVar("uva_dashboard_events", default=None)


class CapturingStore:
    """Wraps the event store: events of a chat message also go to its debug panel."""

    def __init__(self, inner) -> None:
        self.inner = inner

    async def add(self, event: Event) -> None:
        if (bucket := _captured.get()) is not None:
            bucket.append(event)
        if self.inner is not None:
            await self.inner.add(event)

    async def find(self, query: EventQuery) -> list[Event]:
        return await self.inner.find(query)


def mask_settings(settings: Settings) -> list[dict[str, Any]]:
    rows = []
    for name in sorted(type(settings).model_fields):
        value = getattr(settings, name)
        secret = isinstance(value, SecretStr) or bool(SECRET_NAME.search(name))
        if isinstance(value, SecretStr):
            shown = t("dashboard.app.secret_set") if value.get_secret_value() else t("dashboard.app.empty")
        elif value is None:
            shown = t("dashboard.app.unset")
        elif secret:
            shown = t("dashboard.app.secret_set") if str(value) else t("dashboard.app.empty")
        else:
            shown = URI_PASSWORD.sub(r"\g<scheme>\g<user>:••••••@", str(value))
        rows.append({"name": name.upper(), "value": shown, "secret": secret})
    return rows


def reply_json(reply: Reply) -> dict[str, Any]:
    return {
        "text": reply.text,
        "parts": reply.parts,
        "pages": reply.pages,
        "buttons": [{"text": b.text, "data": b.data} for b in reply.buttons],
        "file": reply.filename if reply.document is not None else None,
    }


def event_json(event: Event, tz) -> dict[str, Any]:
    return {
        "ts": event.ts.isoformat(),
        "local": format_local(event.ts, tz, "%d/%m %H:%M:%S"),
        "type": event.type.value,
        "trace_id": event.trace_id,
        "data": event.data,
    }


class DashboardSink(LiveSink):
    """Placeholders for the dashboard chat, streamed to the page."""

    def __init__(self, say, queue: asyncio.Queue, activity: str) -> None:
        super().__init__(say, activity)
        self.queue = queue
        self.n = 0

    async def _post(self, text: str):
        self.n += 1
        await self.queue.put({"type": "post", "id": self.n, "text": text})
        return self.n

    async def _edit(self, handle, text: str, buttons=None) -> None:
        await self.queue.put({"type": "edit", "id": handle, "text": text, "final": True})

    async def _post_reply(self, reply) -> None:
        pass  # the "final" line carries the whole reply


def _reply_kwargs(data: dict[str, Any]) -> dict[str, Any]:
    return {"text": data["text"]}


class ChatIn(BaseModel):
    text: str


class CallbackIn(BaseModel):
    data: str


def create_app(settings: Settings | None = None, services=None, db=None) -> FastAPI:
    """``services``/``db`` are for tests; normally built at startup from settings."""
    settings = settings or (services.settings if services is not None else get_settings())
    state: dict[str, Any] = {}

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        built, database = services, db
        if built is None:
            built = build_services(settings)
            await setup_storage(built)
            database = mongo.get_database(mongo.connect(settings.mongo_uri))
        events.set_store(CapturingStore(events._store))
        state.update(services=built, router=Router(built), db=database)
        yield

    app = FastAPI(title="UVA", docs_url=None, redoc_url=None, lifespan=lifespan)

    @app.middleware("http")
    async def auth(request: Request, call_next):
        password = settings.dashboard_password
        if password is not None and password.get_secret_value():
            header = request.headers.get("authorization", "")
            ok = False
            if header.lower().startswith("basic "):
                try:
                    _, _, given = base64.b64decode(header[6:]).decode().partition(":")
                    ok = secrets.compare_digest(given, password.get_secret_value())
                except Exception:
                    ok = False
            if not ok:
                return JSONResponse(
                    {"error": "password"}, status_code=401, headers={"WWW-Authenticate": 'Basic realm="UVA"'}
                )
        return await call_next(request)

    def current():
        if "services" not in state:
            raise HTTPException(503, t("dashboard.app.starting"))
        return state["services"]

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/api/i18n")
    async def page_texts() -> dict[str, Any]:
        """The page's texts in the bot's language ("dashboard.static.*", prefix stripped)."""
        prefix = "dashboard.static."
        keys = [k for k in catalog(DEFAULT) if k.startswith(prefix)]
        return {"lang": get_language(), "texts": {k.removeprefix(prefix): t(k) for k in keys}}

    @app.get("/api/overview")
    async def overview() -> dict[str, Any]:
        s = current()
        health = {}
        for name, check in s.health_checks.items():
            try:
                health[name] = bool(await check())
            except Exception:
                health[name] = False
        queue = None
        if s.tasks is not None:
            try:
                queue = await s.tasks.queue_size()
            except Exception:
                queue = None
        since = utcnow() - timedelta(hours=24)
        recent = await events._store.find(EventQuery(since=since, limit=5000)) if events._store else []
        by_type: dict[str, int] = {}
        for e in recent:
            by_type[e.type.value] = by_type.get(e.type.value, 0) + 1
        counts: dict[str, Any] = {}
        try:
            counts[t("dashboard.app.counts.memories")] = len(await s.memory.store.all()) if s.memory else None
            counts[t("dashboard.app.counts.goals")] = len(await s.goals.list(_ativo())) if s.goals else None
            counts[t("dashboard.app.counts.notes")] = sum(1 for _ in s.vault.iter_notes()) if s.vault else None
            counts[t("dashboard.app.counts.stickers")] = len(await s.stickers.all()) if s.stickers else None
            counts[t("dashboard.app.counts.projects")] = (
                len(await s.projects.store.recent(500)) if s.projects else None
            )
            from uva.profile import usage

            for u in usage(s):
                counts[u.name] = f"{u.chars}/{u.limit}" + (" ⚠️" if u.over else "")
        except Exception:
            pass
        errors = [event_json(e, s.tz) for e in recent if e.type is EventType.ERROR][-10:]
        return {
            "health": health,
            "queue": queue,
            "dashboard_uptime_s": round(time.time() - STARTED),
            "events_24h": by_type,
            "counts": counts,
            "errors": list(reversed(errors)),
            "now": format_local(utcnow(), s.tz, "%d/%m/%Y %H:%M"),
        }

    @app.get("/api/costs")
    async def costs(days: int = 30) -> dict[str, Any]:
        s = current()
        st = settings
        db = state["db"]
        tz = s.tz
        now = utcnow()
        since = start_of_day(tz, now) - timedelta(days=max(1, min(days, 365)) - 1)
        tzname = st.timezone
        daily = await (await db["llm_usage"].aggregate([
            {"$match": {"ts": {"$gte": since}}},
            {"$group": {
                "_id": {"day": {"$dateToString": {"format": "%Y-%m-%d", "date": "$ts", "timezone": tzname}},
                        "scope": "$scope"},
                "cost": {"$sum": "$cost_usd"}, "calls": {"$sum": 1},
            }},
            {"$sort": {"_id.day": 1}},
        ])).to_list(length=None)
        month = start_of_month(tz, now)

        async def group(field: str) -> list[dict]:
            rows = await (await db["llm_usage"].aggregate([
                {"$match": {"ts": {"$gte": month}}},
                {"$group": {"_id": f"${field}", "cost": {"$sum": "$cost_usd"}, "calls": {"$sum": 1},
                            "input": {"$sum": "$input_tokens"}, "output": {"$sum": "$output_tokens"}}},
                {"$sort": {"cost": -1}},
            ])).to_list(length=None)
            return [{"name": r["_id"], "cost": round(r["cost"], 4), "calls": r["calls"],
                     "input": r["input"], "output": r["output"]} for r in rows]

        spend = await s.budget.spend() if s.budget else None
        projects_today = await s.projects.store.cost_since(start_of_day(tz, now)) if s.projects else 0.0
        return {
            "daily": [{"day": r["_id"]["day"], "scope": r["_id"]["scope"], "cost": round(r["cost"], 4),
                       "calls": r["calls"]} for r in daily],
            "by_purpose": await group("purpose"),
            "by_model": await group("model"),
            "spend": _spend(spend, st),
            "limits": {
                "LLM_DAILY_BUDGET_USD": st.llm_daily_budget_usd,
                "LLM_MONTHLY_BUDGET_USD": st.llm_monthly_budget_usd,
                "PROACTIVE_DAILY_BUDGET_USD": st.proactive_daily_budget_usd,
                "SELFCODE_DAILY_BUDGET_USD": st.selfcode_daily_budget_usd,
                "PROJECT_DAILY_BUDGET_USD": st.project_daily_budget_usd,
                "PROJECT_MAX_BUDGET_USD": st.project_max_budget_usd,
            },
            "projects_today": round(projects_today, 4),
            "expenses": await _expenses(s),
        }

    @app.post("/api/liberar")
    async def liberar(body: dict) -> dict[str, Any]:
        s = current()
        if s.budget is None:
            raise HTTPException(400, t("dashboard.app.no_budget"))
        amount = float(body.get("amount") or 0)
        if not 0 < amount <= 50:
            raise HTTPException(400, t("dashboard.app.amount_range"))
        total = await s.budget.grant_extra(amount)
        return {"extra_today": total}

    @app.get("/api/env")
    async def env() -> list[dict[str, Any]]:
        return mask_settings(settings)

    @app.get("/api/logs")
    async def logs(hours: float = 1, types: str = "", trace: str = "", limit: int = 300) -> list[dict]:
        s = current()
        query = EventQuery(limit=max(1, min(limit, 2000)))
        if trace:
            query.trace_id = trace
        else:
            query.since = utcnow() - timedelta(hours=max(0.05, min(hours, 24 * 90)))
        if types:
            query.types = {EventType(t) for t in types.split(",") if t in EventType._value2member_map_}
        found = await events._store.find(query) if events._store else []
        return [event_json(e, s.tz) for e in reversed(found)]

    @app.get("/api/mcp")
    async def mcp() -> dict[str, Any]:
        """Servers, tools (as UVA sees them) and the last MCP calls."""
        s = current()
        if s.mcp is None:
            return {"enabled": False}
        from uva.commands.mcp import tool_flags
        from uva.mcp.client import McpCallError, McpUnavailable

        error, servers = None, []
        try:
            servers = (await s.mcp.gateway.servers()).get("servers", [])
            await s.mcp.sync(s.actions)
        except (McpUnavailable, McpCallError) as exc:
            error = str(exc)
        since = utcnow() - timedelta(hours=24)
        calls = await events._store.find(EventQuery(since=since, types={EventType.MCP_CALL}, limit=200)) if events._store else []
        return {
            "enabled": True,
            "error": error,
            "config_error": s.mcp.config_error,
            "servers": servers,
            "tools": [
                {"server": i.server, "tool": i.tool, "action": i.action, "status": i.status,
                 "flags": tool_flags(i), "description": i.description[:200]}
                for i in s.mcp.tools
            ],
            "calls": [event_json(e, s.tz) for e in reversed(calls)][:100],
        }

    @app.get("/api/conversation")
    async def conversation() -> dict[str, Any]:
        s = current()
        if s.stm is None:
            return {"turns": [], "refs": []}
        session = await s.stm.session()
        return {
            "turns": [{"role": t.role, "text": t.text, "at": t.at.isoformat(),
                       "local": format_local(t.at, s.tz, "%d/%m %H:%M")} for t in session.turns],
            "refs": [r.model_dump(mode="json") for r in session.refs],
            "focus": session.focus,
        }

    async def _debugged(coro) -> dict[str, Any]:
        bucket: list[Event] = []
        token = _captured.set(bucket)
        started = time.perf_counter()
        try:
            with debug_calls() as calls:
                reply = await coro
        finally:
            _captured.reset(token)
        s = current()
        return {
            "reply": reply_json(reply),
            "debug": {
                "ms": round((time.perf_counter() - started) * 1000),
                "events": [event_json(e, s.tz) for e in bucket],
                "llm_calls": calls,
                "tokens": sum(c["input_tokens"] + c["output_tokens"] for c in calls),
                "cost_usd": round(sum(c["cost_usd"] for c in calls), 6),
            },
        }

    async def _stream(make_work, activity: str) -> StreamingResponse:
        """NDJSON: placeholders ("post"/"edit" lines) as they happen, then the
        "final" line with the reply and its debug data."""
        s = current()
        queue: asyncio.Queue = asyncio.Queue()
        sink = DashboardSink(s.say, queue, activity)

        async def run() -> None:
            try:
                result = await _debugged(with_feedback(sink, make_work()))
                await sink.deliver(Reply(**_reply_kwargs(result["reply"])).messages()[:1])
                await queue.put({"type": "final", **result})
            except Exception as exc:
                await queue.put({"type": "error", "error": repr(exc)[:500]})
            finally:
                await queue.put(None)

        task = asyncio.create_task(run())

        async def lines():
            while (item := await queue.get()) is not None:
                yield json.dumps(item, ensure_ascii=False, default=str) + "\n"
            await task

        return StreamingResponse(lines(), media_type="application/x-ndjson")

    @app.post("/api/chat")
    async def chat(body: ChatIn) -> StreamingResponse:
        text = body.text.strip()
        if not text:
            raise HTTPException(400, t("dashboard.app.empty_message"))
        router: Router = state["router"]
        user = settings.allowed_telegram_user_id

        async def handle():
            turn_source.set("dashboard")  # the conversation log tells the chats apart
            return await router.handle_text(text, user_id=user)

        return await _stream(handle, "executando" if text.startswith("/") else "pensando")

    @app.post("/api/callback")
    async def callback(body: CallbackIn) -> StreamingResponse:
        router: Router = state["router"]
        user = settings.allowed_telegram_user_id
        return await _stream(lambda: router.handle_callback(body.data, user_id=user), "executando")

    return app


def _ativo():
    from uva.goals.store import GoalStatus

    return GoalStatus.ATIVO


def _spend(spend, settings: Settings) -> dict:
    if spend is None:
        return {}
    fields = ("today", "month", "proactive_today", "selfcode_today", "extra_today")
    return {**{k: round(getattr(spend, k), 4) for k in fields}, "daily_limit": spend.daily_limit(settings)}


async def _expenses(s) -> dict[str, Any]:
    if s.expenses is None:
        return {}
    from uva.storage.expenses import format_brl

    month = start_of_month(s.tz, utcnow())
    totals = await s.expenses.totals_by_category(month)
    recent = await s.expenses.recent(10)
    return {
        "month_total": format_brl(sum(c for _, c in totals)) if totals else format_brl(0),
        "by_category": [{"category": c, "total": format_brl(v)} for c, v in totals],
        "recent": [{"id": e.id, "amount": format_brl(e.amount_cents), "category": e.category,
                    "description": e.description, "at": format_local(e.spent_at, s.tz, "%d/%m %H:%M")}
                   for e in recent],
    }


def main() -> None:
    import uvicorn

    from uva.logging import configure_logging

    settings = get_settings()
    configure_logging(settings.log_level)
    uvicorn.run(create_app(settings), host="0.0.0.0", port=8080, log_level="warning")


if __name__ == "__main__":
    main()
