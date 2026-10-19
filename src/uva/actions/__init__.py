"""Actions the LLM can request, and the registry that validates them."""

from uva.actions import (
    builtin,
    context,
    decide,
    documents,
    expenses,
    features,
    files,
    goals,
    history,
    images,
    memory,
    notes,
    projects,
    reminders,
    routines,
    shell,
    skills,
)
from uva.actions.registry import (
    Action,
    ActionContext,
    ActionRegistry,
    ActionResult,
    InvalidActionError,
    RetryableActionError,
)

_MODULES = (builtin, notes, memory, goals, expenses, files, features, projects, routines, reminders, shell, context, decide, documents, images, history, skills)


def build_action_registry() -> ActionRegistry:
    registry = ActionRegistry()
    for module in _MODULES:
        module.register(registry)
    return registry


__all__ = [
    "Action",
    "ActionContext",
    "ActionRegistry",
    "ActionResult",
    "InvalidActionError",
    "RetryableActionError",
    "build_action_registry",
]
