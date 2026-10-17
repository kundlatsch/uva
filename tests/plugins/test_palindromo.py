from uva.plugins.palindromo import check_palindrome, normalize_text
from uva.plugins.testing import run


def test_simple_palindrome():
    assert run(check_palindrome, "reviver").startswith("✅")


def test_ignores_spaces_case_punctuation_and_accents():
    reply = run(
        check_palindrome,
        "Socorram-me,",
        "subi",
        "no",
        "ônibus",
        "em",
        "Marrocos!",
    )
    assert reply.startswith("✅")


def test_non_palindrome():
    assert run(check_palindrome, "banana").startswith("❌")


def test_normalized_flag():
    reply = run(check_palindrome, "--normalizado", "A", "mãe", "te", "ama!!!")
    assert reply.startswith("✅")
    assert "Normalizado: amaeteama" in reply


def test_usage_without_text():
    assert run(check_palindrome).startswith("Uso:")
    assert run(check_palindrome, "--normalizado").startswith("Uso:")


def test_rejects_text_without_letters_or_numbers():
    assert run(check_palindrome, "!!!", "...").startswith("Use pelo menos")


def test_normalize_text_helper():
    assert normalize_text("Áé Í! 123") == "aei123"
