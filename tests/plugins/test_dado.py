import re

from uva.plugins.dado import roll
from uva.plugins.testing import run


def parse_reply(reply: str) -> tuple[int, int]:
    match = re.fullmatch(r"🎲 (\d+) \(d(\d+)\)", reply)
    assert match is not None
    return int(match.group(1)), int(match.group(2))


def test_default_rolls_d6():
    for _ in range(50):
        result, sides = parse_reply(run(roll))
        assert sides == 6
        assert 1 <= result <= 6


def test_custom_sides_rolls_within_range():
    for _ in range(50):
        result, sides = parse_reply(run(roll, "20"))
        assert sides == 20
        assert 1 <= result <= 20


def test_invalid_sides_show_usage():
    assert run(roll, "abc").startswith("Uso:")
    assert run(roll, "1").startswith("Uso:")
    assert run(roll, "1001").startswith("Uso:")
    assert run(roll, "6", "extra").startswith("Uso:")


def test_english_usage():
    assert run(roll, "abc", lang="en") == "Usage: /dice [sides], with sides between 2 and 1000."
    assert parse_reply(run(roll, "20", lang="en"))[1] == 20
