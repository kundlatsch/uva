from zoneinfo import ZoneInfo

import pytest
from fakes import InMemoryMemoryStore, with_llm

from uva.brain.vault import Vault
from uva.index.service import SearchIndex
from uva.memory import MemoryService
from uva.recall import recall
from uva.router import Router
from uva.selfmod.files import FileAccess, FileAccessError
from uva.storage.sqlite import Database

LONG_PROFILE = "# Perfil\n\n" + "\n".join(
    f"- Fato {i}: gosta de jabuticaba, mora em Florianópolis e torce pro Avaí." for i in range(80)
)


@pytest.fixture
def prof_services(services, tmp_path):
    data = tmp_path / "repo" / "data"
    data.mkdir(parents=True)
    (data / "profile.md").write_text(LONG_PROFILE, encoding="utf-8")
    (data / "soul.md").write_text("Sou irônica.", encoding="utf-8")
    services.settings.repo_path = str(tmp_path / "repo")
    services.settings.profile_max_chars = 1000
    services.settings.soul_max_chars = 300
    db = Database(tmp_path / "uva.db")
    db.migrate()
    services.vault = Vault(tmp_path / "brain", ZoneInfo("America/Sao_Paulo"))
    services.vault.ensure_structure()
    services.index = SearchIndex(db, services.vault)
    services.vault.on_change.append(services.index.note_changed)
    services.memory = MemoryService(InMemoryMemoryStore(), services.settings, index=services.index)
    services.files = FileAccess(
        tmp_path / "repo", services.vault.root, None,
        caps={"data/profile.md": 1000, "data/soul.md": 300},
    )
    return services


async def test_writes_over_the_cap_are_refused(prof_services):
    files = prof_services.files
    with pytest.raises(FileAccessError) as exc:
        await files.write("data/profile.md", "x" * 1001, "grow")
    assert "1001" in str(exc.value) and "1000" in str(exc.value) and "/perfil consolidar" in str(exc.value)
    with pytest.raises(FileAccessError):
        await files.append("data/soul.md", "y" * 300, "grow")
    await files.write("data/soul.md", "Curta e grossa.", "ok")
    assert (prof_services.settings.soul_file).read_text(encoding="utf-8") == "Curta e grossa."


async def test_file_write_action_reports_the_refusal(prof_services):
    action = prof_services.actions.get("file_write")
    from uva.actions.registry import ActionContext

    result = await action.handler(
        action.params_model(path="data/profile.md", content="z" * 5000), ActionContext(prof_services)
    )
    assert result.data == {"ok": False} and "5000" in result.text


async def test_perfil_shows_usage(prof_services):
    reply = await Router(prof_services).handle_text("/perfil")
    assert f"profile.md: {len(LONG_PROFILE)}/1000" in reply.text
    assert "acima do limite" in reply.text and "/perfil consolidar" in reply.text
    assert "soul.md: 12/300" in reply.text


async def test_consolidation_keeps_a_full_copy(prof_services):
    core = "# Perfil\n\n- Mora em Florianópolis, torce pro Avaí, adora jabuticaba."
    provider, _ = with_llm(prof_services, core)
    reply = await Router(prof_services).handle_text("/perfil consolidar")
    assert "✅ Perfil consolidado" in reply.text
    profile = prof_services.settings.profile_file.read_text(encoding="utf-8")
    assert profile.startswith(core) and "perfil_detalhado" in profile and len(profile) <= 1000
    # The full previous text: in data/.versions/ and in the brain note.
    versions = list((prof_services.settings.profile_file.parent / ".versions").glob("profile-*.md"))
    assert [v.read_text(encoding="utf-8") for v in versions] == [LONG_PROFILE]
    note = prof_services.vault.read("02-areas/Gustavo/perfil_detalhado.md")
    assert "Fato 79" in note.body
    assert provider.calls[0]["system"].startswith("You rewrite a personal profile")
    # Recall finds the detailed note through the index.
    found = await recall(prof_services, "o que você sabe da jabuticaba e do Avaí?")
    assert "02-areas/Gustavo/perfil_detalhado.md" in found.notes


async def test_consolidation_that_does_not_fit_changes_nothing(prof_services):
    with_llm(prof_services, "a" * 2000, "b" * 2000)
    reply = await Router(prof_services).handle_text("/perfil consolidar")
    assert "deixei como estava" in reply.text
    assert prof_services.settings.profile_file.read_text(encoding="utf-8") == LONG_PROFILE


async def test_status_shows_profile_usage(prof_services):
    reply = await Router(prof_services).handle_text("/status")
    assert "profile.md:" in reply.text and "soul.md:" in reply.text
