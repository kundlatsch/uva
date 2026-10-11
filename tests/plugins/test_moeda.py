import re

from uva.plugins.moeda import flip
from uva.plugins.testing import run


def test_single_flip():
    assert run(flip) in ("🪙 Cara!", "🪙 Coroa!")


def test_many_flips_are_counted():
    reply = run(flip, "5")
    heads, tails = map(int, re.search(r"\((\d+) cara, (\d+) coroa\)", reply).groups())
    assert heads + tails == 5


def test_invalid_count():
    assert run(flip, "abc").startswith("Uso:")
    assert run(flip, "99").startswith("Uso:")
