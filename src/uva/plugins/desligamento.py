from uva.plugins.api import PluginContext, command

TEXTS = {
    "en": {
        "usage": "Usage: /desligamento_pc <linux|windows|macos> confirm. Nothing is executed by the bot.",
        "reply": "✅ Safe shutdown confirmation received for {system}.\nNothing was executed by the bot.\nIf you really want to shut down this machine, review and run manually on your own system:\n{command}",
    },
    "pt": {
        "usage": "Uso: /desligamento_pc <linux|windows|macos> confirmar. Nada é executado pelo bot.",
        "reply": "✅ Confirmação segura de desligamento recebida para {system}.\nNada foi executado pelo bot.\nSe você realmente quer desligar esta máquina, revise e execute manualmente no seu próprio sistema:\n{command}",
    },
}

SYSTEM_NAMES = {
    "linux": "Linux",
    "windows": "Windows",
    "macos": "macOS",
    "mac": "macOS",
    "osx": "macOS",
}

SYSTEM_COMMANDS = {
    "linux": "sudo shutdown -h now",
    "windows": "shutdown /s /t 0",
    "macos": "sudo shutdown -h now",
    "mac": "sudo shutdown -h now",
    "osx": "sudo shutdown -h now",
}

CONFIRMATIONS = ("confirmar", "confirm")


def texts_for(ctx: PluginContext) -> dict[str, str]:
    try:
        lang = ctx.lang
    except AttributeError:
        lang = "pt"
    if lang == "en":
        return TEXTS["en"]
    return TEXTS["pt"]


@command(
    "desligamento_pc",
    {
        "en": "Shows a safe shutdown confirmation by system without executing it",
        "pt": "Mostra uma confirmação segura de desligamento por sistema sem executar",
    },
    usage={"en": "<linux|windows|macos> confirm", "pt": "<linux|windows|macos> confirmar"},
    examples=(
        "desligar pc linux confirmar",
        "desligar pc windows confirmar",
        "desligar pc macos confirmar",
        "safe shutdown linux confirm",
    ),
)
def shutdown_guide(ctx: PluginContext) -> str:
    texts = texts_for(ctx)
    if len(ctx.args) != 2:
        return texts["usage"]

    system_key = ctx.args[0].lower()
    confirmation = ctx.args[1].lower()
    if system_key not in SYSTEM_NAMES or confirmation not in CONFIRMATIONS:
        return texts["usage"]

    return texts["reply"].format(
        system=SYSTEM_NAMES[system_key],
        command=SYSTEM_COMMANDS[system_key],
    )
