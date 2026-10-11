"""Painel somente leitura para memórias e objetivos.

O plugin não acessa armazenamento do bot diretamente; por isso responde com um
resumo seguro e orienta o uso dos comandos nativos quando os dados não estão
expostos ao contexto do plugin.
"""

from uva.plugins.api import PluginContext, command


@command(
    "painel",
    "Mostra um painel curto, somente leitura, de memórias e objetivos",
    usage="",
    examples=("ver painel", "resumo das memórias e objetivos"),
)
def show_panel(ctx: PluginContext) -> str:
    if ctx.args:
        return (
            "Uso: /painel\n"
            "Sem argumentos. Mostra um resumo curto em modo somente leitura."
        )

    lines = [
        "📋 Painel (somente leitura)",
        "Memórias salvas: não disponíveis neste comando isolado.",
        "Objetivos ativos: não disponíveis neste comando isolado.",
        "Detalhes sensíveis não são exibidos aqui.",
        "Nada foi alterado. Para consultas completas, use os comandos nativos de memórias e metas.",
    ]
    return "\n".join(lines)
