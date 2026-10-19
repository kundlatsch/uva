import json
from zoneinfo import ZoneInfo

import pytest
from fakes import InMemoryGoalStore, InMemoryMemoryStore, with_llm

from uva.actions.registry import ActionContext
from uva.brain.vault import Vault
from uva.llm.base import without_context
from uva.llm.prompts import (
    FULL_SCHEMA_ACTIONS,
    assistant_system_prompt,
    describe_action,
    describe_actions,
    describe_commands,
)
from uva.logging import get_logger
from uva.memory import MemoryService
from uva.router import Router

log = get_logger(__name__)


def plan(reply="", *actions) -> str:
    return json.dumps({"actions": [{"type": t, "params": p} for t, p in actions], "reply": reply})


@pytest.fixture
def mem_services(services, tmp_path):
    services.memory = MemoryService(InMemoryMemoryStore(), services.settings)
    services.vault = Vault(tmp_path / "brain", ZoneInfo("America/Sao_Paulo"))
    services.vault.ensure_structure()
    services.goals = InMemoryGoalStore()
    return services


async def test_system_prompt_is_identical_across_messages(mem_services):
    await mem_services.memory.save("cidade", "Mora em Florianópolis", relevance=1.0)
    mem_services.vault.create_note("Viagem a Lisboa", "Fui em maio, choveu.", folder="recursos")
    mem_services.vault.create_note("Receita de pão", "farinha e fermento", folder="recursos")
    provider, _ = with_llm(mem_services, plan("a"), plan("b"))
    router = Router(mem_services)
    await router.handle_text("como foi a viagem a Lisboa?")
    await router.handle_text("qual era a receita de pão?")
    first, second = provider.calls
    assert first["system"] == second["system"]
    # What changed went into the last user message instead.
    assert "Lisboa" in first["messages"][-1].content and "Lisboa" not in first["system"]
    assert "fermento" in second["messages"][-1].content
    assert "Now:" not in first["system"] and "Now:" in first["messages"][-1].content
    assert without_context(second["messages"][-1].content) == "qual era a receita de pão?"


def test_prompt_size_is_smaller_than_full_schemas(services):
    actions = list(services.actions)
    full = describe_actions(actions)
    compact = describe_actions(actions, FULL_SCHEMA_ACTIONS)
    old_commands = "\n".join(
        f"- /{c.name} {c.usage}".rstrip() + f": {c.description}" for c in services.registry
    )
    new_commands = describe_commands(services.registry)
    prompt = assistant_system_prompt(actions=actions, registry=services.registry)
    # The same prompt with full schemas and usage text (as before).
    before = len(prompt) - len(compact) + len(full) - len(new_commands) + len(old_commands)
    log.info(
        "prompt_size",
        actions_full=len(full), actions_compact=len(compact),
        commands_old=len(old_commands), commands_new=len(new_commands),
        system_before=before, system_after=len(prompt),
    )
    assert len(compact) < len(full) and len(prompt) < before
    # Every action is still listed, by name.
    assert all(f"- {a.name}" in compact for a in actions)
    # Compact lines show parameter names; optional ones end with "?".
    assert "- decide(" in compact and "params:" not in compact.split("- decide(")[1].split("\n")[0]


async def test_action_help_returns_the_full_schema(services):
    action = services.actions.get("action_help")
    params = action.params_model(names=["decide", "nope"])
    result = await action.handler(params, ActionContext(services))
    assert result.text.startswith(describe_action(services.actions.get("decide")))
    assert "- nope: essa ação não existe" in result.text
    assert action.lookup


async def test_compact_actions_are_still_accepted(services):
    # The validator accepts every action, also those without a full schema in the prompt.
    assert "memory_forget" not in FULL_SCHEMA_ACTIONS
    action, _ = services.actions.validate("memory_forget", {"key": "cidade"})
    assert action.name == "memory_forget"


def test_commands_are_one_line_with_aliases(services):
    text = describe_commands(services.registry)
    assert len(text.splitlines()) == len(services.registry)
    help_cmd = services.registry.get("help")
    if help_cmd.aliases:
        assert f"/{help_cmd.aliases[0]}" in text


def test_no_stale_third_party_promise(services):
    prompt = assistant_system_prompt(actions=list(services.actions), registry=services.registry)
    assert "/rodar" not in prompt and "is not supported" in prompt
