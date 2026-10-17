from uva.plugins.maiusculo import uppercase
from uva.plugins.testing import run


def test_converts_text_to_uppercase():
    assert run(uppercase, "bom", "dia") == "BOM DIA"


def test_preserves_numbers_and_punctuation():
    assert run(uppercase, "Rua", "7,", "apto", "12!") == "RUA 7, APTO 12!"


def test_converts_accented_letters():
    assert run(uppercase, "olá,", "joão!") == "OLÁ, JOÃO!"


def test_requires_text():
    assert run(uppercase) == "Uso: /maiusculo <texto>"


def test_english_usage():
    assert run(uppercase, lang="en") == "Usage: /uppercase <text>"
    assert run(uppercase, "good", "morning", lang="en") == "GOOD MORNING"
