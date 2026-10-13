from zoneinfo import ZoneInfo

import pytest

from uva.brain.vault import Vault
from uva.router import Router


@pytest.fixture
def router(services, tmp_path) -> Router:
    vault = Vault(tmp_path / "brain", ZoneInfo("America/Sao_Paulo"))
    vault.ensure_structure()
    vault.create_note("Receita de pão", "farinha, água, sal", folder="recursos", tags=["cozinha"])
    vault.create_folder("areas", "Saúde")
    hidden = vault.root / ".obsidian"
    hidden.mkdir()
    (hidden / "segredo.md").write_text("não mostrar", encoding="utf-8")
    services.vault = vault
    return Router(services)


async def test_tree_lists_folders_and_notes(router):
    text = (await router.handle_text("/brain")).text
    assert text.startswith("🧠 brain")
    assert "📁 03-recursos/ (1 nota(s))" in text and "· receita_de_pao.md" in text
    assert "02-areas/" in text and "Saúde/Saúde.md" in text
    assert ".obsidian" not in text and "segredo" not in text


async def test_folder_listing_by_para_name_or_path(router):
    text = (await router.handle_text("/brain recursos")).text
    assert text.startswith("📂 03-recursos") and "📝 receita_de_pao.md" in text
    assert "📁 Saúde/ · 1 nota(s)" in (await router.handle_text("/brain 02-areas")).text


async def test_note_shows_raw_content_with_front_matter(router):
    text = (await router.handle_text("/brain receita de pão")).text
    assert text.startswith("📝 03-recursos/receita_de_pao.md")
    assert "tags:\n- cozinha" in text and "farinha, água, sal" in text


async def test_empty_note(router):
    assert "(arquivo vazio)" in (await router.handle_text("/brain 02-areas/Saúde/Saúde.md")).text


async def test_long_note_is_paginated(router, services):
    body = "\n".join(f"linha {i}" for i in range(1, 121))
    services.vault.create_note("Longa", body, folder="inbox")
    first = await router.handle_text("/ver Longa")
    assert first.document is None
    assert first.text.startswith("📝 00-inbox/longa.md") and "linha 1\n" in first.text
    assert first.text.endswith("— página 1/4 —")
    assert [b.text for b in first.buttons] == ["Próxima ▶"]

    second = await router.handle_callback(first.buttons[0].data)
    assert second.edit and second.text.startswith("📝 00-inbox/longa.md")  # header on every page
    assert "— página 2/4 —" in second.text
    assert [b.text for b in second.buttons] == ["◀ Anterior", "Próxima ▶"]

    back = await router.handle_callback(second.buttons[0].data)
    assert "— página 1/4 —" in back.text
    assert "expirou" in (await router.handle_callback("pg:naoexiste:1")).text


@pytest.mark.parametrize("ref", [".obsidian/segredo", "../../etc/passwd", "05-anexos/foto.jpg", "nada disso"])
async def test_hidden_outside_or_non_markdown_is_not_shown(router, ref):
    text = (await router.handle_text(f"/brain {ref}")).text
    assert "Não achei" in text or text.startswith("📂")  # never the content
    assert "não mostrar" not in text
