"""Actions the LLM can request, and the registry that validates them."""

from uva.actions import (
    builtin,
    context,
    decide,
    expenses,
    features,
    files,
    goals,
    images,
    memory,
    notes,
    projects,
)
from uva.actions.registry import (
    Action,
    ActionContext,
    ActionRegistry,
    ActionResult,
    InvalidActionError,
    RetryableActionError,
)

_MODULES = (builtin, notes, memory, goals, expenses, files, features, projects, context, decide, images)


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
