from uva.plugins.painel import panel
from uva.plugins.testing import StaticData, run


def test_usage_when_args_are_given():
    assert run(panel, "extra") == "Uso: /painel"


def test_empty_panel_shows_nada_and_zero_expenses():
    reply = run(panel)

    assert "Memória de trabalho\nnada" in reply
    assert "Objetivos ativos\nnada" in reply
    assert "Contexto recente\nnada" in reply
    assert "Gastos\n- hoje: R$ 0,00\n- mês: R$ 0,00" in reply


def test_panel_uses_read_only_data():
    data = StaticData(
        working_memory=[
            {"key": "k1", "value": "v1", "category": "teste"},
            {"key": "k2", "value": "v2", "category": "teste"},
            {"key": "k3", "value": "v3", "category": "teste"},
            {"key": "k4", "value": "v4", "category": "teste"},
            {"key": "k5", "value": "v5", "category": "teste"},
            {"key": "k6", "value": "v6", "category": "teste"},
        ],
        goals=[
            {
                "id": "g1",
                "title": "Meta A",
                "status": "ativo",
                "priority": 1,
                "kind": "rotina",
                "next_run": "12/10 09:00",
                "last_attempt": "tentativa ok",
                "last_attempt_at": "11/10 08:00",
                "run_count": 2,
            },
        ],
        recent_refs=[
            {
                "ref": "r1",
                "tipo": "nota",
                "rotulo": "Ref A",
                "status": "feito",
                "quando": "11/10 10:00",
            },
        ],
    )

    reply = run(panel, data=data)

    assert "- k1: v1" in reply
    assert "- k5: v5" in reply
    assert "- k6: v6" not in reply
    assert "- Meta A (próxima: 12/10 09:00; última: tentativa ok em 11/10 08:00)" in reply
    assert "- Ref A — feito" in reply
