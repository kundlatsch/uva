"""Fallback tier 3: the LLM reads the message and answers with JSON actions."""

from __future__ import annotations

import time
from collections import deque

from uva.actions.registry import (
    Action,
    ActionContext,
    ActionResult,
    CommandRunner,
    InvalidActionError,
    RetryableActionError,
)
from uva.llm.base import LLMError, LLMMessage
from uva.llm.budget import Scope
from uva.llm.plan import PLAN_SCHEMA, PlanError, parse_plan
from uva.llm.prompts import assistant_system_prompt
from uva.memory.service import render_memory
from uva.logging import get_logger
from uva.logging.events import EventType, current_trace_id, emit
from uva.replies import Reply
from uva.services import Services

LLM_DISABLED = (
    "Não entendi, e a LLM não está configurada (LLM_API_KEY no .env). "
    "Envie /help para ver os comandos."
)
LLM_FAILED = "Não consegui falar com a LLM agora. Tente de novo em instantes (/log erros)."
BAD_PLAN = "Me enrolei para entender isso. Pode reformular? (o erro foi registrado)"
HISTORY_CHARS = 2000

log = get_logger(__name__)

# Budget scope of every call made from a conversation.
INTERACTIVE = Scope.INTERACTIVE


class Agent:
    def __init__(self, services: Services, run_command: CommandRunner | None = None) -> None:
        self.services = services
        self.run_command = run_command
        self.history: deque[LLMMessage] = deque(maxlen=services.settings.llm_history_messages)

    async def handle(self, text: str) -> Reply:
        services = self.services
        if services.llm is None or services.actions is None:
            await emit(EventType.ROUTE, tier="llm_disabled")
            return Reply(LLM_DISABLED)

        if services.budget is not None:
            status = await services.budget.check(INTERACTIVE)
            if not status.allowed:
                await emit(EventType.BUDGET, scope=INTERACTIVE.value, blocked=True, reason=status.reason)
                return Reply(
                    f"Não posso usar a LLM agora: {status.reason}. "
                    "Os comandos continuam funcionando (/help)."
                )

        actions = list(services.actions)
        system = assistant_system_prompt(
            actions=actions,
            registry=services.registry,
            tz=services.tz,
            working_memory=await self._working_memory(),
        )
        messages = [*self.history, LLMMessage("user", text)]
        try:
            response = await services.llm.complete(
                messages,
                purpose="plan",
                scope=INTERACTIVE,
                system=system,
                json_schema=PLAN_SCHEMA,
            )
        except LLMError:
            return Reply(LLM_FAILED)

        try:
            plan = parse_plan(response.text)
            calls = [services.actions.validate(c.type, c.params) for c in plan.actions]
        except (PlanError, InvalidActionError) as exc:
            # Nothing from a broken answer is executed.
            await emit(EventType.ERROR, where="plan", error=str(exc), raw=response.text)
            return Reply(BAD_PLAN)

        await emit(EventType.ROUTE, tier="llm", plan=plan.model_dump())
        ctx = ActionContext(services, INTERACTIVE, self.run_command)
        results = [await self._run(action, params, ctx) for action, params in calls]

        reply = compose_reply(plan.reply, results)
        self._remember(text, reply)
        return Reply(reply)

    async def _working_memory(self) -> str:
        if self.services.memory is None:
            return ""
        try:
            return render_memory(await self.services.memory.working_memory())
        except Exception as exc:  # Mongo down: answer without memory
            log.warning("working_memory_unavailable", error=str(exc))
            return ""

    async def _run(self, action: Action, params, ctx: ActionContext) -> ActionResult:
        """Slow actions go to the background queue; the rest run right away."""
        tasks = self.services.tasks
        if action.long_running and tasks is not None:
            try:
                record = await tasks.enqueue(action.name, params.model_dump(), current_trace_id())
            except Exception as exc:
                # Queue down: still do the work, just synchronously.
                log.warning("enqueue_failed", action=action.name, error=str(exc))
            else:
                await emit(
                    EventType.TASK_ENQUEUED,
                    task_id=record.id,
                    action=action.name,
                    params=record.params,
                )
                return ActionResult(
                    text=f"{action.progress_text} (tarefa {record.id}; /cancelar {record.id})",
                    data={"task_id": record.id},
                )
        return await execute_action(action, params, ctx)

    def _remember(self, user_text: str, reply: str) -> None:
        self.history.append(LLMMessage("user", user_text[:HISTORY_CHARS]))
        self.history.append(LLMMessage("assistant", reply[:HISTORY_CHARS]))


async def execute_action(action: Action, params, ctx: ActionContext) -> ActionResult:
    """Run one validated action, logging the outcome.

    Never raises, except RetryableActionError inside background tasks.
    """
    started = time.perf_counter()
    try:
        result = await action.handler(params, ctx)
        ok, error = True, None
    except RetryableActionError as exc:
        await emit(
            EventType.ACTION,
            name=action.name,
            params=params.model_dump(),
            ok=False,
            error=str(exc),
            retrying=True,
            scope=ctx.scope.value,
        )
        raise
    except Exception as exc:
        result = ActionResult(text=f"Não consegui executar {action.name}.")
        ok, error = False, repr(exc)
    await emit(
        EventType.ACTION,
        name=action.name,
        params=params.model_dump(),
        ok=ok,
        error=error,
        scope=ctx.scope.value,
        duration_ms=round((time.perf_counter() - started) * 1000),
        result=result.data or result.text,
    )
    return result


def compose_reply(plan_reply: str, results: list[ActionResult]) -> str:
    parts: list[str] = []
    for part in [plan_reply, *(r.text for r in results)]:
        part = part.strip()
        if part and part not in parts:  # the LLM sometimes repeats "reply" as an action
            parts.append(part)
    return "\n\n".join(parts) or "Feito."
