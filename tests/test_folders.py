import json
from zoneinfo import ZoneInfo

import pytest
from fakes import with_llm

from uva.brain.vault import Vault
from uva.router import Router


@pytest.fixture
def vault(tmp_path) -> Vault:
    v = Vault(tmp_path / "brain", ZoneInfo("America/Sao_Paulo"))
    v.ensure_structure()
    return v


def test_create_folder_with_empty_note(vault):
    folder, placeholder = vault.create_folder("areas", "Saúde")
    assert folder == "02-areas/Saúde" and placeholder == "02-areas/Saúde/Saúde.md"
    assert (vault.root / placeholder).read_bytes() == b""  # empty, like a Python __init__.py
    assert vault.read(placeholder).title == "Saúde"  # still readable/searchable as a note


def test_existing_folder_is_left_alone(vault):
    vault.create_folder("areas", "Saúde")
    (vault.root / "02-areas/Saúde/Treino.md").write_text("meu treino", encoding="utf-8")
    assert vault.create_folder("areas", "Saúde") == ("02-areas/Saúde", None)
    assert (vault.root / "02-areas/Saúde/Treino.md").read_text(encoding="utf-8") == "meu treino"


def test_nested_folder_and_note_in_subfolder(vault):
    folder, _ = vault.create_folder("areas", "Saúde/Exercícios")
    assert folder == "02-areas/Saúde/Exercícios"
    note = vault.create_note("Plano", "3x por semana", folder="areas", subfolder="Saúde/Exercícios")
    assert note.path == "02-areas/Saúde/Exercícios/Plano.md"


@pytest.mark.parametrize("name", [".obsidian", "Saúde/.oculta", "../fora", "a/b/c/d"])
def test_hidden_escaping_or_too_deep_folders_are_refused(vault, name):
    with pytest.raises(ValueError):
        vault.create_folder("areas", name)


async def test_llm_picks_the_place_and_creates_the_folder(services, vault):
    services.vault = vault
    plan = {
        "actions": [{"type": "folder_create", "params": {"name": "Finanças", "folder": "areas"}}],
        "reply": "Criei em áreas: dinheiro é responsabilidade contínua.",
    }
    with_llm(services, json.dumps(plan))
    reply = await Router(services).handle_text("cria uma pasta de finanças")
    assert reply.parts == [
        "Criei em áreas: dinheiro é responsabilidade contínua.",
        "📁 Pasta criada: 02-areas/Finanças (com a nota vazia 02-areas/Finanças/Finanças.md)",
    ]


async def test_folder_with_content_creates_note_inside(services, vault):
    services.vault = vault
    plan = {
        "actions": [
            {"type": "folder_create", "params": {"name": "Viagem Japão", "folder": "projetos"}},
            {"type": "note_create", "params": {"title": "Roteiro", "body": "Tóquio, Kyoto",
                                               "folder": "projetos", "subfolder": "Viagem Japão"}},
        ],
        "reply": "Projeto com prazo, então foi para projetos.",
    }
    with_llm(services, json.dumps(plan))
    await Router(services).handle_text("cria uma pasta pra viagem ao Japão com o roteiro Tóquio, Kyoto")
    assert (vault.root / "01-projetos/Viagem Japão/Roteiro.md").exists()
    assert (vault.root / "01-projetos/Viagem Japão/Viagem Japão.md").exists()
