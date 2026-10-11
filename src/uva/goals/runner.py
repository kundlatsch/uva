"""The proactive loop: work on the user's goals when nobody is around.

Every PROACTIVE_INTERVAL_MINUTES the beat triggers :func:`proactive_tick`:
  1. pick the most important active goal that is due (nothing due: return);
  2. if the bot is busy (user chatting, tasks running): skip this turn;
  3. if the proactive budget is used up: skip;
  4. run up to PROACTIVE_MAX_STEPS LLM rounds with only proactive-safe
     actions (search, notes, memory) and a token cap;
  5. write the outcome to a note in 01-projetos/ and reschedule the goal.
With PROACTIVE_NOTIFY the owner gets a summary of each finished run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

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
from uva.logging.events import EventType, current_trace_id, emit, trace
from uva.memory.service import render_memory
from uva.replies import Reply
from uva.selfmod.features import FeatureStatus
from uva.services import Services
from uva.stm.refs import remember_ref
from uva.text import normalize
from uva.tasks.runner import Notifier

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
    needs_feature: str | None = None  # /feature text when the goal needs a new command
    feature_id: str | None = None  # self-code request made for it, if any
    feature_note: str = ""  # why no request was made (or that one already exists)
    feature_later: bool = False  # couldn't ask now (busy/budget) but may next time

    @property
    def summary(self) -> str:
        return " ".join(s for s in self.summaries if s).strip() or (self.error or "Sem progresso.")


async def proactive_tick(
    services: Services, now: datetime | None = None, notify: Notifier | None = None
) -> str:
    """One turn of the loop. Returns what happened (also logged as an event).

    With ``notify`` (PROACTIVE_NOTIFY), the owner gets a short summary of each
    finished run; skipped turns are never announced.
    """
    now = now or utcnow()
    if services.goals is None or services.llm is None or services.actions is None:
        return "disabled"

    due = await services.goals.due(now)
    if not due:
        log.debug("proactive_idle")
        return "idle"
    goal = due[0]

    with trace():
        # At most one goal run per PROACTIVE_INTERVAL_MINUTES (beat polls more often).
        spacing = timedelta(minutes=services.settings.proactive_interval_minutes)
        last = max((g.last_run_at for g in await services.goals.list() if g.last_run_at), default=None)
        if last is not None and now - last < spacing:
            await _attempted(services, goal, now, "pulado: intervalo entre execuções")
            return "interval"

        if services.busy is not None:
            reasons = await services.busy.reasons()
            if reasons:
                await _skipped(goal, "busy", busy=reasons)
                await _attempted(services, goal, now, f"pulado: ocupado ({', '.join(reasons)})")
                return "busy"

        if services.budget is not None:
            budget = await services.budget.check(Scope.PROACTIVE)
            if not budget.allowed:
                await emit(
                    EventType.BUDGET, scope=Scope.PROACTIVE.value, blocked=True, reason=budget.reason
                )
                await _skipped(goal, "budget")
                await _attempted(services, goal, now, f"pulado: {budget.reason}")
                return "budget"

        busy_key = f"goal:{goal.id}"
        if services.busy is not None:
            await services.busy.mark(busy_key, services.settings.task_time_limit_seconds)
        try:
            run = await run_goal(services, goal)
            if run.needs_feature:
                request = await _request_feature(services, goal, run.needs_feature)
                run.feature_id, run.feature_note, run.feature_later = request
            note = _write_note(services, run, now)
            await _reschedule(services, run, note, now)
        finally:
            if services.busy is not None:
                await services.busy.clear(busy_key)

        if run.needs_feature:
            status = "needs_feature"
        else:
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
            needs_feature=run.needs_feature,
        )
        if notify is not None:
            await notify(Reply(describe_run(run, status, note)))
        return status


def describe_run(run: GoalRun, status: str, note: str | None) -> str:
    if run.feature_id:
        lines = [
            f"🎯 Objetivo \"{run.goal.title}\" precisava de um comando novo, então pedi:",
            f"🛠️ feature {run.feature_id}: {run.needs_feature}",
            "Te aviso quando ficar pronta (ou se der ruim). /features acompanha.",
        ]
        if note:
            lines.append(f"📝 {note}")
        return "\n".join(lines)
    if run.needs_feature:
        lines = [
            f"🎯 Objetivo \"{run.goal.title}\" precisa de um comando novo, e isso eu não faço sozinha.",
            f"Se quiser, manda: /feature {run.needs_feature}",
            f"({run.feature_note})" if run.feature_note else "",
            f"Pausei o objetivo; /retomar {run.goal.id} reativa." if _pauses(run) else "",
        ]
        lines = [line for line in lines if line]
        if note:
            lines.append(f"📝 {note}")
        return "\n".join(lines)
    label = {"done": "rodou", "concluido": "concluído ✅", "failed": "falhou ⚠️"}.get(status, status)
    summary = run.summary if len(run.summary) <= 400 else run.summary[:399] + "…"
    lines = [f"🎯 Objetivo \"{run.goal.title}\" {label}", summary]
    if note:
        lines.append(f"📝 {note}")
    return "\n".join(lines)


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
            assert isinstance(plan, GoalPlan)
            if plan.needs_feature:
                feature = plan.needs_feature.strip().removeprefix("/feature").strip()
                run.needs_feature, run.done = feature, True
                run.summaries.append(f"Precisa de um comando novo: /feature {feature}")
                break
            results: list[ActionResult] = [await execute_action(a, p, ctx) for a, p in calls]
            run.outputs.extend(r.text for r in results if r.text)
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


async def _attempted(services: Services, goal: Goal, now: datetime, result: str) -> None:
    assert services.goals is not None
    await services.goals.update(goal.id, last_attempt_at=now, last_attempt=result)


async def _skipped(goal: Goal, reason: str, **extra) -> None:
    await emit(
        EventType.GOAL_RUN, goal_id=goal.id, title=goal.title, status="skipped", reason=reason, **extra
    )


def _pauses(run: GoalRun) -> bool:
    """Pause when a feature is needed and nothing will come of it by waiting."""
    return run.needs_feature is not None and run.feature_id is None and not run.feature_later


async def _request_feature(
    services: Services, goal: Goal, description: str
) -> tuple[str | None, str, bool]:
    """At most one self-code request per run, never twice for the same thing.

    Returns (feature id, note, retry_later).
    """
    settings = services.settings
    features = services.features
    if not settings.proactive_allow_selfcode:
        return None, "auto-código por objetivos está desligado (PROACTIVE_ALLOW_SELFCODE)", False
    if features is None or services.repo is None:
        return None, "auto-código não está configurado", False
    wanted = normalize(description)
    for record in await features.store.recent(50):
        same = record.goal_id == goal.id or normalize(record.description) == wanted
        if same and record.status in (FeatureStatus.QUEUED, FeatureStatus.RUNNING, FeatureStatus.MERGED):
            state = "pronta" if record.status is FeatureStatus.MERGED else "em construção"
            return None, f"já existe a feature {record.id} para isso ({state})", True
        if record.status in (FeatureStatus.QUEUED, FeatureStatus.RUNNING):
            return None, f"outra feature ({record.id}) está em construção; tento de novo depois", True
    if services.budget is not None:
        status = await services.budget.check(Scope.SELFCODE)
        if not status.allowed:
            return None, f"orçamento de auto-código: {status.reason}", True
    record = await features.request(description, current_trace_id(), goal_id=goal.id)
    await emit(EventType.SELFCODE, op="requested", feature_id=record.id, goal_id=goal.id, description=description)
    await remember_ref(services, "feature", record.id, f"feature: {description[:60]}", "na fila")
    return record.id, "", False


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
        "last_attempt_at": now,
        "last_attempt": "ok" if run.ok else f"falhou: {run.error}",
        "needs_feature": run.needs_feature,
    }
    if run.feature_id:
        fields["last_attempt"] = f"pediu a feature {run.feature_id}"
    elif run.needs_feature:
        fields["last_attempt"] = f"precisa de /feature {run.needs_feature}"
        if _pauses(run):
            # Don't come back every day to say the same thing.
            fields["status"] = GoalStatus.PAUSADO
    elif _completes(run):
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
