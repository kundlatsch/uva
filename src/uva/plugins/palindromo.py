"""Plugin para checar se um texto é palíndromo."""

import unicodedata

from uva.plugins.api import PluginContext, command


NORMALIZED_FLAGS = ("--normalizado", "--normalizada", "--normalized", "-n")

TEXTS = {
    "en": {
        "usage": "Usage: /palindrome [--normalized] <text>. E.g.: /palindrome racecar",
        "no_letters": "Use at least one letter or number to check for a palindrome.",
        "yes": "✅ It's a palindrome: “{text}”.",
        "no": "❌ Not a palindrome: “{text}”.",
        "normalized": "Normalized: {normalized}",
    },
    "pt": {
        "usage": "Uso: /palindromo [--normalizado] <texto>. Ex.: /palindromo reviver",
        "no_letters": "Use pelo menos uma letra ou número para checar um palíndromo.",
        "yes": "✅ É palíndromo: “{text}”.",
        "no": "❌ Não é palíndromo: “{text}”.",
        "normalized": "Normalizado: {normalized}",
    },
}


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
    {"en": "Checks whether a text is a palindrome", "pt": "Checa se um texto é palíndromo"},
    aliases=("palíndromo", "palindrome"),
    usage="[--normalizado] <texto>",
    examples=(
        "reviver",
        "socorram me subi no onibus em marrocos",
        "--normalizado A mãe te ama",
    ),
)
def check_palindrome(ctx: PluginContext) -> str:
    texts = TEXTS.get(ctx.lang, TEXTS["en"])
    text, show_normalized = parse_text(ctx.args)
    if not text:
        return texts["usage"]

    normalized = normalize_text(text)
    if not normalized:
        return texts["no_letters"]

    is_palindrome = normalized == normalized[::-1]
    reply = texts["yes" if is_palindrome else "no"].format(text=text)

    if show_normalized:
        reply += "\n" + texts["normalized"].format(normalized=normalized)
    return reply
