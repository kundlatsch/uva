"""Celery application (worker + beat). Start with ``celery -A uva.tasks.app ...``."""

from __future__ import annotations

from datetime import timedelta

from celery import Celery, signals
from celery.schedules import crontab

from uva import i18n
from uva.config import get_settings
from uva.logging import configure_logging

settings = get_settings()
# Failure notices may go out before Services (which also sets it) are built.
i18n.set_language(settings.bot_language)

app = Celery("uva", broker=settings.redis_url, backend=settings.redis_url, include=["uva.tasks.jobs"])
app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    result_expires=timedelta(days=1),
    timezone=settings.timezone,
    worker_prefetch_multiplier=1,
    task_time_limit=settings.task_time_limit_seconds,
    task_soft_time_limit=max(30, settings.task_time_limit_seconds - 30),
    broker_connection_retry_on_startup=True,
    worker_hijack_root_logger=False,
    # /projeto runs Claude Code in its own worker (container "projects").
    task_routes={"uva.project_build": {"queue": "projects"}},
)

# Periodic jobs.
app.conf.beat_schedule = {
    "heartbeat": {"task": "uva.heartbeat", "schedule": timedelta(minutes=1)},
    "memory-maintenance": {
        "task": "uva.memory_maintenance",
        "schedule": crontab(hour=settings.memory_maintenance_hour, minute=0),
    },
}
# Routines: checked every minute; missed slots (PC off) run once when back.
app.conf.beat_schedule["routines-tick"] = {"task": "uva.routines_tick", "schedule": crontab(minute="*")}
app.conf.beat_schedule["index-sync"] = {"task": "uva.index_sync", "schedule": timedelta(minutes=10)}
app.conf.beat_schedule["review-tick"] = {"task": "uva.review_tick", "schedule": crontab(minute="*/2")}
app.conf.beat_schedule["journal-digest"] = {
    "task": "uva.journal_digest",
    "schedule": crontab(hour=settings.memory_maintenance_hour, minute=10),
}
# Weekly brain check: Sunday, after the daily maintenance (it only reports).
app.conf.beat_schedule["brain-lint"] = {
    "task": "uva.brain_lint",
    "schedule": crontab(day_of_week="sun", hour=settings.memory_maintenance_hour, minute=20),
}
app.conf.beat_schedule["stm-promotion"] = {
    "task": "uva.stm_promotion",
    "schedule": timedelta(minutes=30),
}
if settings.proactive_interval_minutes > 0:
    # A wall-clock crontab, not a timedelta from beat's start: beat is recreated on
    # every deploy/boot, and a 60-min timedelta restarted its countdown each time
    # (goals never ran). The loop itself enforces PROACTIVE_INTERVAL_MINUTES.
    poll = max(1, settings.proactive_poll_minutes)
    # Offset from the goal tick, so a goal run (which marks the bot busy) starts first.
    app.conf.beat_schedule["initiative-tick"] = {
        "task": "uva.initiative_tick",
        "schedule": crontab(minute=f"{poll // 2}-59/{poll}" if poll > 1 else "*"),
    }
    app.conf.beat_schedule["proactive-tick"] = {
        "task": "uva.proactive_tick",
        "schedule": crontab(minute=f"*/{max(1, settings.proactive_poll_minutes)}"),
    }


@signals.worker_ready.connect
def _sweep_after_restart(sender=None, **_: object) -> None:
    """Tasks this worker was running when it stopped are failed (and the user told)."""
    hostname = getattr(sender, "hostname", "") or ""
    if hostname.startswith("projects@"):
        return  # /projeto has its own records
    app.send_task("uva.recover_tasks", countdown=10)


@signals.setup_logging.connect
def _setup_logging(**_: object) -> None:
    # Same JSON logs as the bot instead of Celery's own format.
    configure_logging(settings.log_level)
