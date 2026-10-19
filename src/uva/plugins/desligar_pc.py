"""Safe shutdown confirmation plugin for UVA.

UVA plugins are sandboxed and must not execute local operating-system actions.
This command therefore shows the exact shutdown action, asks for explicit
confirmation, and keeps the bot safe by not running the command.
"""

from uva.plugins.api import PluginContext, command

TEXTS = {
    "en": {
        "usage": "Usage: /desligar_pc [windows|linux|macos] confirmar",
        "intro": (
            "⚠️ Safe shutdown confirmation\n"
            "UVA plugins cannot run operating-system commands. I can show what would be done "
            "and require explicit confirmation without turning anything off.\n\n"
            "Supported systems: {systems}\n"
            "{usage}"
        ),
        "confirm": (
            "⚠️ Confirm computer shutdown\n"
            "System: {system}\n"
            "Intended action: request shutdown in about 1 minute.\n"
            "Command that would be used: `{command}`\n\n"
            "To confirm: /desligar_pc {key} confirmar\n"
            "To cancel: /desligar_pc cancelar"
        ),
        "confirmed_no_execute": (
            "✅ Confirmation received.\n"
            "For safety, this plugin did NOT execute any command on the computer.\n"
            "What an authorized local integration would do:\n"
            "System: {system}\n"
            "Command: `{command}`"
        ),
        "cancelled": "Cancelled. Nothing was done.",
        "invalid": "Invalid option. {usage}",
        "need_system": "I cannot detect the current system from the plugin sandbox. Choose one explicitly. {usage}",
    },
    "pt": {
        "usage": "Uso: /desligar_pc [windows|linux|macos] confirmar",
        "intro": (
            "⚠️ Confirmação segura de desligamento\n"
            "Plugins da UVA não podem executar comandos do sistema. Posso mostrar o que seria feito "
            "e exigir confirmação explícita sem desligar nada.\n\n"
            "Sistemas suportados: {systems}\n"
            "{usage}"
        ),
        "confirm": (
            "⚠️ Confirme o desligamento do computador\n"
            "Sistema: {system}\n"
            "Ação pretendida: solicitar desligamento em cerca de 1 minuto.\n"
            "Comando que seria usado: `{command}`\n\n"
            "Para confirmar: /desligar_pc {key} confirmar\n"
            "Para cancelar: /desligar_pc cancelar"
        ),
        "confirmed_no_execute": (
            "✅ Confirmação recebida.\n"
            "Por segurança, este plugin NÃO executou nenhum comando no computador.\n"
            "O que uma integração local autorizada faria:\n"
            "Sistema: {system}\n"
            "Comando: `{command}`"
        ),
        "cancelled": "Cancelado. Nenhuma ação foi executada.",
        "invalid": "Opção inválida. {usage}",
        "need_system": "Não consigo detectar o sistema atual dentro do sandbox do plugin. Escolha um explicitamente. {usage}",
    },
}

SYSTEMS = {
    "windows": {"en": "Windows", "pt": "Windows", "command": "shutdown /s /t 60"},
    "linux": {"en": "Linux", "pt": "Linux", "command": "shutdown -h +1"},
    "macos": {"en": "macOS", "pt": "macOS", "command": "shutdown -h +1"},
}

CONFIRM_WORDS = ("confirmar", "confirma", "sim", "yes", "confirm")
CANCEL_WORDS = ("cancelar", "cancela", "não", "nao", "no", "cancel")


def system_key(value: str) -> str:
    text = value.strip().lower()
    if text in ("win", "windows"):
        return "windows"
    if text in ("linux", "gnu/linux"):
        return "linux"
    if text in ("mac", "macos", "macós", "osx", "darwin"):
        return "macos"
    return ""


def systems_line(lang: str) -> str:
    names = [SYSTEMS[key][lang] for key in ("windows", "linux", "macos")]
    return ", ".join(names)


@command(
    "desligar_pc",
    {
        "en": "Shows a safe shutdown confirmation for Windows, Linux or macOS",
        "pt": "Mostra uma confirmação segura de desligamento para Windows, Linux ou macOS",
    },
    usage={"en": "[windows|linux|macos] confirm", "pt": "[windows|linux|macos] confirmar"},
    examples=("desligar o pc", "desligar computador", "shutdown computer"),
)
def shutdown_pc(ctx: PluginContext) -> str:
    lang = ctx.lang if ctx.lang in ("en", "pt") else "en"
    texts = TEXTS.get(lang, TEXTS["en"])

    if not ctx.args:
        return texts["intro"].format(systems=systems_line(lang), usage=texts["usage"])

    if len(ctx.args) > 2:
        return texts["invalid"].format(usage=texts["usage"])

    first = ctx.args[0].strip().lower()
    if first in CANCEL_WORDS:
        return texts["cancelled"]
    if first in CONFIRM_WORDS:
        return texts["need_system"].format(usage=texts["usage"])

    key = system_key(ctx.args[0])
    if not key:
        return texts["invalid"].format(usage=texts["usage"])

    system_name = SYSTEMS[key][lang]
    command_text = SYSTEMS[key]["command"]

    if len(ctx.args) == 1:
        return texts["confirm"].format(system=system_name, command=command_text, key=key)

    second = ctx.args[1].strip().lower()
    if second in CANCEL_WORDS:
        return texts["cancelled"]
    if second not in CONFIRM_WORDS:
        return texts["invalid"].format(usage=texts["usage"])

    return texts["confirmed_no_execute"].format(system=system_name, command=command_text)
