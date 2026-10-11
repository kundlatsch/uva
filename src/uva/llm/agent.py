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
from uva.stm.refs import remember_ref, task_label

LLM_DISABLED = "(a LLM não está configurada: falta LLM_API_KEY no .env)"
QUOTA_MARKERS = ("429", "402", "quota", "insufficient", "credit", "billing", "rate limit")
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
            return Reply(f"{services.say('nao_entendi')}\n{LLM_DISABLED}")

        if services.budget is not None:
            status = await services.budget.check(INTERACTIVE)
            if not status.allowed:
                await emit(EventType.BUDGET, scope=INTERACTIVE.value, blocked=True, reason=status.reason)
                return Reply(f"{services.say('sem_credito')}\n({status.reason})")

        actions = list(services.actions)
        history, recent_context = await self._session_context()
        system = assistant_system_prompt(
            actions=actions,
            registry=services.registry,
            tz=services.tz,
            working_memory=await self._working_memory(),
            recent_context=recent_context,
        )
        messages = [*history, LLMMessage("user", text)]
        try:
            response = await services.llm.complete(
                messages,
                purpose="plan",
                scope=INTERACTIVE,
                system=system,
                json_schema=PLAN_SCHEMA,
            )
        except LLMError as exc:
            if is_quota_error(exc):
                return Reply(f"{services.say('sem_credito')}\n(o provedor recusou por limite/crédito)")
            return Reply(services.say("llm_erro"))

        try:
            plan = parse_plan(response.text)
            calls = [services.actions.validate(c.type, c.params) for c in plan.actions]
        except (PlanError, InvalidActionError) as exc:
            # Nothing from a broken answer is executed.
            await emit(EventType.ERROR, where="plan", error=str(exc), raw=response.text)
            return Reply(services.say("llm_erro"))

        await emit(EventType.ROUTE, tier="llm", plan=plan.model_dump())
        ctx = ActionContext(services, INTERACTIVE, self.run_command)
        results = [await self._run(action, params, ctx) for action, params in calls]

        reply = compose_reply(plan.reply, results) or services.say("confirmacao")
        self._remember(text, reply)
        return Reply(reply)

    async def _session_context(self) -> tuple[list[LLMMessage], str]:
        """Recent turns (as messages) and the "Contexto recente" block."""
        stm = self.services.stm
        limit = self.services.settings.llm_history_messages
        if stm is None:
            return list(self.history), ""
        try:
            session = await stm.session()
        except Exception as exc:  # Redis down: fall back to the in-process history
            log.warning("stm_unavailable", error=str(exc))
            return list(self.history), ""
        return stm.history(session, limit), stm.context_block(session, self.services.tz)

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
                await remember_ref(
                    self.services,
                    "pesquisa" if action.name == "search" else "tarefa",
                    record.id,
                    task_label(action.name, record.params),
                    "na fila",
                )
                progress = action.progress_text
                if action.progress_phrase:
                    progress = self.services.say(action.progress_phrase, **record.params)
                return ActionResult(
                    text=f"{progress} (tarefa {record.id}; /cancelar {record.id})",
                    data={"task_id": record.id},
                )
        return await execute_action(action, params, ctx)

    def _remember(self, user_text: str, reply: str) -> None:
        # Only used when there is no short-term memory (the router records turns there).
        if self.services.stm is not None:
            return
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
    return "\n\n".join(parts)


def is_quota_error(exc: Exception) -> bool:
    """Provider refused for lack of credit / rate limit (402/429)."""
    text = str(exc).lower()
    return any(marker in text for marker in QUOTA_MARKERS)
