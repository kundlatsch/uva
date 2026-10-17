"""Plugin para checar se um texto é palíndromo."""

import unicodedata

from uva.plugins.api import PluginContext, command


NORMALIZED_FLAGS = ("--normalizado", "--normalizada", "-n")


def normalize_text(text: str) -> str:
    """Remove acentos, pontuação e espaços; deixa só letras/números em minúsculas."""
    folded = text.casefold()
    decomposed = unicodedata.normalize("NFD", folded)
    without_accents = "".join(
        char for char in decomposed if unicodedata.category(char) != "Mn"
    )
    return "".join(char for char in without_accents if char.isalnum())


def parse_text(args: list[str]) -> tuple[str, bool]:
    show_normalized = False
    words = []
    for arg in args:
        if arg in NORMALIZED_FLAGS:
            show_normalized = True
        else:
            words.append(arg)
    return " ".join(words).strip(), show_normalized


@command(
    "palindromo",
    "Checa se um texto é palíndromo",
    aliases=("palindrome",),
    usage="[--normalizado] <texto>",
    examples=(
        "reviver",
        "socorram me subi no onibus em marrocos",
        "--normalizado A mãe te ama",
    ),
)
def check_palindrome(ctx: PluginContext) -> str:
    text, show_normalized = parse_text(ctx.args)
    if not text:
        return "Uso: /palindromo [--normalizado] <texto>. Ex.: /palindromo reviver"

    normalized = normalize_text(text)
    if not normalized:
        return "Use pelo menos uma letra ou número para checar um palíndromo."

    is_palindrome = normalized == normalized[::-1]
    if is_palindrome:
        reply = f"✅ É palíndromo: “{text}”."
    else:
        reply = f"❌ Não é palíndromo: “{text}”."

    if show_normalized:
        reply += f"\nNormalizado: {normalized}"
    return reply
