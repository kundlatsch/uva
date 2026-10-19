"""Action registry: the only things the LLM is allowed to make the bot do.

An action is a Python function with a name, a description and a Pydantic
model for its parameters. The LLM answers with action names + params; we
validate them against this registry and run the matching function. The
LLM never runs free-form code.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, RootModel, ValidationError

from uva.i18n import t
from uva.llm.budget import Scope

if TYPE_CHECKING:
    from uva.replies import Reply
    from uva.services import Services

# Lets an action run a bot command (used by run_command).
CommandRunner = Callable[[str, list[str]], Awaitable["Reply"]]


@dataclass(slots=True)
class ActionContext:
    services: Services
    scope: Scope = Scope.INTERACTIVE
    run_command: CommandRunner | None = None
    # True inside a background task that still has retries left: transient
    # failures should raise RetryableActionError instead of giving up.
    allow_retry: bool = False
    # The user confirmed this exact call (a write the injection scanner held back).
    confirmed: bool = False


@dataclass(slots=True)
class ActionResult:
    text: str = ""  # appended to the reply sent to the user
    data: dict[str, Any] = field(default_factory=dict)  # logged / fed back to the LLM
    buttons: list[Any] = field(default_factory=list)  # e.g. a confirmation (replies.Button)


ActionHandler = Callable[[Any, ActionContext], Awaitable[ActionResult]]


class JsonArgs(RootModel[dict[str, Any]]):
    """Params of an action described by a JSON Schema (MCP tools), already
    validated against it; model_dump() gives the plain dict back."""


@dataclass(frozen=True, slots=True)
class Action:
    name: str
    description: str
    params_model: type[BaseModel]
    handler: ActionHandler
    # Allowed in the unattended goal loop (no side effects outside notes/memory).
    proactive_safe: bool = False
    # Slow actions (network, LLM summaries) run as background tasks when possible.
    long_running: bool = False
    # Shown to the user while a long-running action works in the background
    # (None: a generic "working on it" in the language set at startup).
    progress_text: str | None = None
    # Phrase category used instead of progress_text when set (e.g. "pesquisando").
    progress_phrase: str | None = None
    # Read-only lookups (note_read, recall...): in a conversation their output goes
    # back to the LLM, which then answers in the same turn.
    lookup: bool = False
    # Actions registered at runtime from a JSON Schema (MCP tools): validated with
    # jsonschema and wrapped in JsonArgs; params_model is then JsonArgs.
    json_schema: dict[str, Any] | None = field(default=None, compare=False)
    # Actions replaced together (replace_group), e.g. "mcp".
    group: str | None = None
    # Free-form details for the group's own code (MCP: server, tool, flags).
    meta: Mapping[str, Any] | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        if self.progress_text is None:
            object.__setattr__(self, "progress_text", t("actions.registry.working"))


class InvalidActionError(ValueError):
    pass


class RetryableActionError(RuntimeError):
    """Transient failure (network...): the background task should retry."""


class ActionRegistry:
    def __init__(self) -> None:
        self._actions: dict[str, Action] = {}

    def register(self, action: Action) -> Action:
        if action.name in self._actions:
            raise ValueError(f"Action already registered: {action.name!r}")
        self._actions[action.name] = action
        return action

    def action(
        self,
        name: str,
        description: str,
        params_model: type[BaseModel],
        *,
        proactive_safe: bool = False,
        long_running: bool = False,
        progress_text: str | None = None,
        progress_phrase: str | None = None,
        lookup: bool = False,
    ) -> Callable[[ActionHandler], ActionHandler]:
        def decorator(func: ActionHandler) -> ActionHandler:
            self.register(
                Action(
                    name,
                    description,
                    params_model,
                    func,
                    proactive_safe,
                    long_running,
                    progress_text,
                    progress_phrase,
                    lookup,
                )
            )
            return func

        return decorator

    def replace_group(self, group: str, actions: Iterable[Action]) -> None:
        """Drop every action of ``group`` and register ``actions`` in their place."""
        self._actions = {n: a for n, a in self._actions.items() if a.group != group}
        for action in actions:
            if action.group != group:
                raise ValueError(f"Action {action.name!r} is not in group {group!r}")
            self.register(action)

    def group(self, group: str) -> list[Action]:
        return [a for a in self if a.group == group]

    def get(self, name: str) -> Action | None:
        return self._actions.get(name)

    def __iter__(self) -> Iterator[Action]:
        return iter(self._actions.values())

    def subset(self, names: set[str] | None = None, *, proactive_only: bool = False) -> list[Action]:
        return [
            a
            for a in self
            if (names is None or a.name in names) and (a.proactive_safe or not proactive_only)
        ]

    def validate(
        self, name: str, params: dict[str, Any], *, allowed: set[str] | None = None
    ) -> tuple[Action, BaseModel]:
        """Resolve and validate one call, raising InvalidActionError."""
        action = self.get(name)
        if action is None or (allowed is not None and name not in allowed):
            raise InvalidActionError(t("actions.registry.unknown_action", name=repr(name)))
        if action.json_schema is not None:
            return action, _validate_json(action, params)
        try:
            return action, action.params_model.model_validate(params)
        except ValidationError as exc:
            raise InvalidActionError(t("actions.registry.invalid_params", name=name, error=exc)) from exc


def _validate_json(action: Action, params: dict[str, Any]) -> JsonArgs:
    import jsonschema

    if not isinstance(params, dict):
        raise InvalidActionError(t("actions.registry.invalid_params", name=action.name, error="params must be an object"))
    validator_class = jsonschema.validators.validator_for(action.json_schema, default=jsonschema.Draft202012Validator)
    errors = sorted(validator_class(action.json_schema).iter_errors(params), key=lambda e: list(e.path))
    if errors:
        detail = "; ".join(f"{'/'.join(map(str, e.path)) or 'params'}: {e.message}" for e in errors[:3])
        raise InvalidActionError(t("actions.registry.invalid_params", name=action.name, error=detail))
    return JsonArgs(params)
