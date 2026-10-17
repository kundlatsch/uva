from uva.plugins.desligar_pc import shutdown_pc
from uva.plugins.testing import run


def test_without_args_explains_safe_confirmation():
    reply = run(shutdown_pc)
    assert "Confirmação segura" in reply
    assert "Windows" in reply
    assert "Linux" in reply
    assert "macOS" in reply
    assert "Uso: /desligar_pc" in reply


def test_system_preview_shows_exact_command_and_confirmation():
    reply = run(shutdown_pc, "windows")
    assert "Confirme o desligamento" in reply
    assert "shutdown /s /t 60" in reply
    assert "/desligar_pc windows confirmar" in reply


def test_confirmation_is_safe_and_does_not_execute():
    reply = run(shutdown_pc, "linux", "confirmar")
    assert "Confirmação recebida" in reply
    assert "NÃO executou" in reply
    assert "shutdown -h +1" in reply


def test_cancel_and_invalid_input():
    assert run(shutdown_pc, "cancelar").startswith("Cancelado")
    assert run(shutdown_pc, "solaris").startswith("Opção inválida")
    assert run(shutdown_pc, "linux", "agora").startswith("Opção inválida")


def test_english_replies():
    intro = run(shutdown_pc, lang="en")
    assert "Safe shutdown confirmation" in intro
    assert "Usage: /desligar_pc" in intro

    preview = run(shutdown_pc, "macos", lang="en")
    assert "Confirm computer shutdown" in preview
    assert "shutdown -h +1" in preview

    confirmed = run(shutdown_pc, "windows", "confirm", lang="en")
    assert "did NOT execute" in confirmed
    assert "shutdown /s /t 60" in confirmed
