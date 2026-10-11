from uva.plugins.painel import show_panel
from uva.plugins.testing import run


def test_panel_reply_is_short_and_read_only():
    reply = run(show_panel)

    assert "Painel" in reply
    assert "somente leitura" in reply
    assert "Memórias salvas" in reply
    assert "Objetivos ativos" in reply
    assert "Nada foi alterado" in reply
    assert len(reply) < 500


def test_panel_rejects_arguments_with_usage():
    reply = run(show_panel, "2")

    assert reply.startswith("Uso: /painel")
    assert "Sem argumentos" in reply
