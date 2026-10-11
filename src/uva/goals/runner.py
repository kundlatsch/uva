"""The proactive loop: work on the user's goals when nobody is around.

Every PROACTIVE_INTERVAL_MINUTES the beat triggers :func:`proactive_tick`:
  1. pick the most important active goal that is due (nothing due: return);
  2. if the bot is busy (user chatting, tasks running): skip this turn;
  3. if the proactive budget is used up: skip;
  4. run up to PROACTIVE_MAX_STEPS LLM rounds with only proactive-safe
     actions (search, notes, memory) and a token cap;
  5. write the outcome to a note in 01-projetos/ and reschedule the goal.
It never sends Telegram messages.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from uva.actions.registry import ActionContext, ActionResult, InvalidActionError
from uva.clock import format_local, utcnow
from uva.goals.store import MAX_RESULTS_KEPT, Goal, GoalKind, GoalResult, GoalStatus
from uva.llm.agent import execute_action
from uva.llm.base import LLMError, LLMMessage
from uva.llm.budget import Scope
from uva.llm.client import metered
from uva.llm.plan import GOAL_PLAN_SCHEMA, GoalPlan, PlanError, parse_plan
from uva.llm.prompts import goal_system_prompt
from uva.logging import get_logger
from uva.logging.events import EventType, emit, trace
from uva.memory.service import render_memory
from uva.services import Services

log = get_logger(__name__)

RESULT_CHARS = 1500  # per action result fed back to the LLM


@dataclass(slots=True)
class GoalRun:
    goal: Goal
    ok: bool = True
    done: bool = False
    steps: int = 0
    tokens: int = 0
    summaries: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def summary(self) -> str:
        return " ".join(s for s in self.summaries if s).strip() or (self.error or "Sem progresso.")


async def proactive_tick(services: Services, now: datetime | None = None) -> str:
    """One turn of the loop. Returns what happened (also logged as an event)."""
    now = now or utcnow()
    if services.goals is None or services.llm is None or services.actions is None:
        return "disabled"

    due = await services.goals.due(now)
    if not due:
        log.debug("proactive_idle")
        return "idle"
    goal = due[0]

    with trace():
        if services.busy is not None:
            reasons = await services.busy.reasons()
            if reasons:
                await _skipped(goal, "busy", busy=reasons)
                return "busy"

        if services.budget is not None:
            budget = await services.budget.check(Scope.PROACTIVE)
            if not budget.allowed:
                await emit(
                    EventType.BUDGET, scope=Scope.PROACTIVE.value, blocked=True, reason=budget.reason
                )
                await _skipped(goal, "budget")
                return "budget"

        busy_key = f"goal:{goal.id}"
        if services.busy is not None:
            await services.busy.mark(busy_key, services.settings.task_time_limit_seconds)
        try:
            run = await run_goal(services, goal)
            note = _write_note(services, run, now)
            await _reschedule(services, run, note, now)
        finally:
            if services.busy is not None:
                await services.busy.clear(busy_key)

        status = "failed" if not run.ok else ("concluido" if _completes(run) else "done")
        await emit(
            EventType.GOAL_RUN,
            goal_id=goal.id,
            title=goal.title,
            status=status,
            steps=run.steps,
            tokens=run.tokens,
            note=note,
            summary=run.summary,
            error=run.error,
        )
        return status


async def run_goal(services: Services, goal: Goal) -> GoalRun:
    """The LLM loop for one goal, limited in steps and tokens."""
    assert services.llm is not None and services.actions is not None
    settings = services.settings
    safe_actions = services.actions.subset(proactive_only=True)
    allowed = {a.name for a in safe_actions}
    system = goal_system_prompt(
        goal_title=goal.title,
        goal_description=goal.description,
        previous=_previous_results(goal, services),
        actions=safe_actions,
        tz=services.tz,
        max_steps=settings.proactive_max_steps,
        working_memory=await _working_memory(services),
    )
    ctx = ActionContext(services, Scope.PROACTIVE)  # no run_command, no retries
    messages = [LLMMessage("user", "Execute a primeira rodada.")]
    run = GoalRun(goal)

    with metered() as meter:
        while run.steps < settings.proactive_max_steps:
            if meter.total >= settings.proactive_max_tokens_per_run:
                run.error = "limite de tokens da execução atingido"
                break
            run.steps += 1
            try:
                response = await services.llm.complete(
                    messages,
                    purpose="goal",
                    scope=Scope.PROACTIVE,
                    model=services.llm.heavy_model,
                    system=system,
                    json_schema=GOAL_PLAN_SCHEMA,
                )
                plan = parse_plan(response.text, GoalPlan)
                calls = [
                    services.actions.validate(c.type, c.params, allowed=allowed)
                    for c in plan.actions
                ]
            except LLMError as exc:
                run.ok, run.error = False, f"LLM indisponível: {exc}"
                break
            except (PlanError, InvalidActionError) as exc:
                await emit(
                    EventType.ERROR,
                    where="goal_plan",
                    goal_id=goal.id,
                    error=str(exc),
                    raw=response.text,
                )
                run.ok, run.error = False, f"resposta inválida da LLM: {exc}"
                break

            run.summaries.append(plan.reply.strip())
            results: list[ActionResult] = [await execute_action(a, p, ctx) for a, p in calls]
            run.outputs.extend(r.text for r in results if r.text)
            assert isinstance(plan, GoalPlan)
            if plan.done or not calls:
                run.done = True
                break
            feedback = "\n\n".join(
                f"[{a.name}] {r.text[:RESULT_CHARS] or r.data}" for (a, _), r in zip(calls, results)
            )
            messages += [
                LLMMessage("assistant", response.text),
                LLMMessage("user", f"Resultados das ações:\n{feedback}\n\nExecute a próxima rodada."),
            ]
        run.tokens = meter.total
    return run


async def _skipped(goal: Goal, reason: str, **extra) -> None:
    await emit(
        EventType.GOAL_RUN, goal_id=goal.id, title=goal.title, status="skipped", reason=reason, **extra
    )


def _completes(run: GoalRun) -> bool:
    return run.ok and run.done and run.goal.kind is GoalKind.UNICO


def _write_note(services: Services, run: GoalRun, now: datetime) -> str | None:
    """Append this run to the goal's project note (created on first run)."""
    vault = services.vault
    if vault is None:
        return None
    title = f"Objetivo - {run.goal.title}"
    when = format_local(now, services.tz, "%d/%m/%Y %H:%M")
    body = [f"**Execução {run.goal.run_count + 1}** ({when}): {run.summary}"]
    body += [f"\n{output}" for output in run.outputs]
    text = "\n".join(body)
    try:
        note = vault.append(title, text, now=now)
    except LookupError:
        intro = run.goal.description or "Objetivo acompanhado pela UVA."
        note = vault.create_note(title, intro, folder="projetos", tags=["objetivo"], now=now)
        note = vault.append(note.path, text, now=now)
    vault.append_daily(f"Objetivo trabalhado: [[{note.title}]]", now=now)
    return note.path


async def _reschedule(services: Services, run: GoalRun, note: str | None, now: datetime) -> None:
    assert services.goals is not None
    goal = run.goal
    results = [*goal.results, GoalResult(at=now, summary=run.summary, note=note, ok=run.ok)]
    fields: dict = {
        "last_run_at": now,
        "next_run_at": now + goal.min_interval,
        "run_count": goal.run_count + 1,
        "results": results[-MAX_RESULTS_KEPT:],
    }
    if _completes(run):
        fields["status"] = GoalStatus.CONCLUIDO
    await services.goals.update(goal.id, **fields)


def _previous_results(goal: Goal, services: Services) -> str:
    return "\n".join(
        f"- {format_local(r.at, services.tz, '%d/%m %H:%M')}: {r.summary}" for r in goal.results[-5:]
    )


async def _working_memory(services: Services) -> str:
    if services.memory is None:
        return ""
    try:
        return render_memory(await services.memory.working_memory())
    except Exception:
        return ""
