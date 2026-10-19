"""web_fetch: read a page (or a PDF, text, JSON) at a URL."""

from __future__ import annotations

from pydantic import BaseModel, Field

from uva.actions.registry import ActionContext, ActionRegistry, ActionResult
from uva.i18n import t
from uva.logging.events import EventType, emit


class WebFetchParams(BaseModel):
    url: str = Field(description="The http(s) URL to read")


def register(registry: ActionRegistry) -> None:
    @registry.action(
        "web_fetch",
        "Reads a web page (or a PDF, text or JSON) at a URL and returns its text",
        WebFetchParams,
        lookup=True,
        proactive_safe=True,
    )
    async def web_fetch(params: WebFetchParams, ctx: ActionContext) -> ActionResult:
        from uva.web import fetch as web

        try:
            result = await web.fetch_for(ctx.services, params.url)
        except web.FetchError as exc:
            await emit(EventType.ROUTE, tier="web_fetch_refused", url=params.url[:200], code=exc.code)
            return ActionResult(text=t("actions.web.failed", url=params.url, error=web.describe_error(exc)),
                                data={"ok": False, "code": exc.code})
        return ActionResult(
            text=web.for_llm(result),
            data={"url": result.final_url, "chars": len(result.text), "truncated": result.truncated},
        )
