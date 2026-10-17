from uva.plugins.desligamento import shutdown_guide
from uva.plugins.testing import run


def test_linux_confirmation_message_is_safe():
    reply = run(shutdown_guide, "linux", "confirmar")
    assert "Linux" in reply
    assert "Nada foi executado" in reply
    assert "sudo shutdown -h now" in reply


def test_windows_confirmation_message_is_safe():
    reply = run(shutdown_guide, "windows", "confirmar")
    assert "Windows" in reply
    assert "Nada foi executado" in reply
    assert "shutdown /s /t 0" in reply


def test_macos_confirmation_message_is_safe():
    reply = run(shutdown_guide, "macos", "confirmar")
    assert "macOS" in reply
    assert "Nada foi executado" in reply
    assert "sudo shutdown -h now" in reply


def test_invalid_arguments_show_usage():
    assert run(shutdown_guide).startswith("Uso:")
    assert run(shutdown_guide, "linux").startswith("Uso:")
    assert run(shutdown_guide, "bsd", "confirmar").startswith("Uso:")
    assert run(shutdown_guide, "linux", "agora").startswith("Uso:")


def test_english_replies():
    reply = run(shutdown_guide, "linux", "confirm", lang="en")
    assert "Linux" in reply
    assert "Nothing was executed" in reply
    assert "sudo shutdown -h now" in reply
    assert run(shutdown_guide, "linux", lang="en").startswith("Usage:")


class LegacyContext:
    args = ["linux", "confirmar"]


def test_context_without_lang_uses_safe_portuguese_fallback():
    reply = shutdown_guide(LegacyContext())
    assert "Linux" in reply
    assert "Nada foi executado" in reply
