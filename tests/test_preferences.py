from datetime import date

import pytest
from fakes import InMemoryMemoryStore, with_llm
from test_profile import prof_services  # noqa: F401  (fixture)

from uva import preferences
from uva.actions.registry import ActionContext
from uva.memory import MemoryService
from uva.router import Router

TODAY = date(2026, 10, 19)
PROFILE = """# Perfil

Identidade
- Mora em Florianópolis.
- Torce pro Avaí.

## Preferences
- Sempre responder em tópicos curtos <!-- observed: 2026-01-10 | status: active -->
- Nunca usar emojis <!-- observed: 2026-03-01 | status: active -->
"""


class InMemoryHistory:
    def __init__(self):
        self.rows = []

    async def add(self, entry):
        self.rows.append(dict(entry))

    async def for_key(self, key, limit=20):
        return [r for r in reversed(self.rows) if r["key"] == key][:limit]


@pytest.fixture
def mem(services):
    services.memory = MemoryService(InMemoryMemoryStore(), services.settings, history=InMemoryHistory())
    return services


# --- memory history ------------------------------------------------------------------


async def test_history_on_update_replace_and_forget(mem):
    memory = mem.memory
    await memory.save("cidade", "Florianópolis")
    await memory.save("cidade", "Florianópolis")  # same value: nothing to keep
    await memory.update("cidade", value="Lisboa")
    await memory.save("cidade", "Porto")
    await memory.forget("cidade")
    rows = memory.history.rows
    assert [(r["old_value"], r["reason"]) for r in rows] == [
        ("Florianópolis", "update"), ("Lisboa", "replace"), ("Porto", "forget"),
    ]
    assert all(r["key"] == "cidade" and r["superseded_at"] for r in rows)


async def test_memoria_history_command(mem):
    router = Router(mem)
    await router.handle_text("/lembrar time Figueirense")
    await router.handle_text("/lembrar time Avaí")
    text = (await router.handle_text("/memoria time historico")).text
    assert "Histórico de time" in text and "agora: Avaí" in text and "Figueirense (substituído)" in text
    await router.handle_text("/esquecer time")
    text = (await router.handle_text("/memoria time history")).text
    assert "Avaí (esquecido)" in text and "agora:" not in text
    assert "Não lembro" in (await router.handle_text("/memoria nada historico")).text


# --- preferences ------------------------------------------------------------------------


def test_supersede_in_place():
    text, old = preferences.supersede(PROFILE, "Sempre responder em parágrafos curtos", "tópicos curtos", TODAY)
    lines = text.splitlines()
    i = next(n for n, line in enumerate(lines) if "tópicos curtos" in line)
    assert "status: superseded" in lines[i] and "observed: 2026-01-10" in lines[i]
    assert lines[i + 1] == "- Sempre responder em parágrafos curtos <!-- observed: 2026-10-19 | status: active -->"
    assert old.text == "Sempre responder em tópicos curtos"
    # Without a match it goes at the end of the section; without a section, one is created.
    text, old = preferences.supersede(PROFILE, "Prefere exemplos em Python", "", TODAY)
    assert old is None and text.splitlines()[-1].startswith("- Prefere exemplos em Python")
    text, _ = preferences.supersede("# Perfil\n- x\n", "Nunca usar gírias", "", TODAY)
    assert "## Preferences\n- Nunca usar gírias <!-- observed: 2026-10-19 | status: active -->" in text


def test_drop_old_superseded_and_loss():
    text, _ = preferences.supersede(PROFILE, "Usar emojis às vezes", "emojis", TODAY)
    kept, dropped = preferences.drop_superseded(text, date(2026, 12, 1))  # observed 2026-03-01: old
    assert dropped == 1 and "Nunca usar emojis" not in kept and "Usar emojis às vezes" in kept
    assert preferences.drop_superseded(text, TODAY.replace(month=4))[1] == 0  # still recent
    assert preferences.items(text).count("Nunca usar emojis") == 0  # superseded lines don't count
    assert preferences.loss(PROFILE, PROFILE) == 0
    assert preferences.loss(PROFILE, "# Perfil\n- Mora em Florianópolis.") == pytest.approx(0.75)


async def test_profile_patch_preference(prof_services):  # noqa: F811
    prof_services.settings.profile_file.write_text(PROFILE, encoding="utf-8")
    prof_services.settings.profile_max_chars = 4000
    prof_services.files.caps["data/profile.md"] = 4000
    action = prof_services.actions.get("profile_patch")
    params = action.params_model(preference="Nunca usar gírias", supersedes="")
    result = await action.handler(params, ActionContext(prof_services))
    assert result.data["ok"]
    params = action.params_model(preference="Sempre responder em parágrafos", supersedes="tópicos curtos")
    await action.handler(params, ActionContext(prof_services))
    profile = prof_services.settings.profile_file.read_text(encoding="utf-8")
    assert "tópicos curtos <!-- observed: 2026-01-10 | status: superseded -->" in profile
    assert profile.count("status: active") == 3
    empty = await action.handler(action.params_model(), ActionContext(prof_services))
    assert not empty.data["ok"]


async def test_consolidation_prunes_old_superseded_without_the_llm(prof_services):  # noqa: F811
    text, _ = preferences.supersede(PROFILE, "Usar emojis às vezes", "emojis", date(2026, 3, 5))
    prof_services.settings.profile_file.write_text(text, encoding="utf-8")
    prof_services.settings.profile_max_chars = 4000
    provider, _ = with_llm(prof_services)
    reply = await Router(prof_services).handle_text("/perfil consolidar")
    assert "Tirei 1 preferência" in reply.text and provider.calls == []
    assert "Nunca usar emojis" not in prof_services.settings.profile_file.read_text(encoding="utf-8")


async def test_consolidation_keeps_preferences_and_refuses_big_losses(prof_services):  # noqa: F811
    facts = "\n".join(f"- Fato {i}: coleciona selos raros da região {i}." for i in range(60))
    profile = f"# Perfil\n\n{facts}\n\n## Preferences\n" + preferences.render("Sempre ser direto", TODAY) + "\n"
    prof_services.settings.profile_file.write_text(profile, encoding="utf-8")
    with_llm(prof_services, "# Perfil\n- Gosta de futebol.")  # loses every fact
    reply = await Router(prof_services).handle_text("/perfil consolidar")
    assert "Deixei o perfil como estava" in reply.text and "100%" in reply.text
    assert prof_services.settings.profile_file.read_text(encoding="utf-8") == profile

    provider, _ = with_llm(prof_services, "# Perfil\n- Fatos 0 a 59: coleciona selos raros da região.")
    reply = await Router(prof_services).handle_text("/perfil consolidar")
    assert "✅ Perfil consolidado" in reply.text
    new = prof_services.settings.profile_file.read_text(encoding="utf-8")
    assert "## Preferences\n- Sempre ser direto <!-- observed: 2026-10-19 | status: active -->" in new
    assert "Sempre ser direto" not in provider.calls[0]["messages"][0].content  # kept out of the rewrite
