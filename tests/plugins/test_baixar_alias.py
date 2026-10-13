from uva.plugins.baixar_alias import baixar_alias
from uva.plugins.testing import run


def test_explains_reserved_alias():
    reply = run(baixar_alias)
    assert "/baixar" in reply
    assert "/baixar_video" in reply
    assert "aliases" in reply


def test_rejects_arguments():
    assert run(baixar_alias, "https://exemplo.test/video").startswith("Uso:")
