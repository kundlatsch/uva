"""Actions the LLM can request, and the registry that validates them."""

from uva.actions import builtin
from uva.actions.registry import (
    Action,
    ActionContext,
    ActionRegistry,
    ActionResult,
    InvalidActionError,
)

_MODULES = (builtin,)


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
    "build_action_registry",
]
