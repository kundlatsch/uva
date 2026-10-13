"""Ajuda sobre o alias reservado para o comando de baixar vídeo."""

from uva.plugins.api import PluginContext, command


@command(
    "baixar_alias",
    "Explica como ativar o alias /baixar para /baixar_video",
    usage="",
    examples=("alias baixar",),
)
def baixar_alias(ctx: PluginContext) -> str:
    if ctx.args:
        return "Uso: /baixar_alias"
    return (
        "Não posso criar um novo comando /baixar por plugin porque esse nome já está reservado. "
        "Para manter o mesmo comportamento de /baixar_video, o alias precisa ser incluído "
        "diretamente no comando existente, com aliases=(\"baixar\",)."
    )
