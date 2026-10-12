from uva.plugins.pokemon import build_target, display_name, format_pokemon, pokemon
from uva.plugins.testing import run


def test_usage_without_args():
    assert run(pokemon).startswith("Uso: /pokemon")


def test_build_target_accepts_name_id_and_forms():
    assert build_target(["pikachu"]) == "pikachu"
    assert build_target(["25"]) == "25"
    assert build_target(["ninetales", "alola"]) == "ninetales-alola"
    assert build_target(["mewtwo", "mega", "x"]) == "mewtwo-mega-x"


def test_build_target_rejects_invalid_id_with_form():
    assert build_target(["25", "alola"]) == ""
    assert build_target(["0"]) == ""


def test_display_name_formats_slug():
    assert display_name("ninetales-alola") == "Ninetales Alola"
    assert display_name("mewtwo-mega-x") == "Mewtwo Mega X"


def test_format_pokemon_includes_main_sections():
    data = {
        "id": 25,
        "name": "pikachu",
        "height": 4,
        "weight": 60,
        "types": [{"slot": 1, "type": {"name": "electric"}}],
        "abilities": [
            {"slot": 1, "is_hidden": False, "ability": {"name": "static"}},
            {"slot": 3, "is_hidden": True, "ability": {"name": "lightning-rod"}},
        ],
        "stats": [
            {"base_stat": 35, "stat": {"name": "hp"}},
            {"base_stat": 90, "stat": {"name": "speed"}},
        ],
        "sprites": {
            "front_default": "https://example.test/front.png",
            "other": {"official-artwork": {"front_default": "https://example.test/art.png"}},
        },
    }

    reply = format_pokemon(data, ["pikachu", "pikachu-rock-star"])

    assert "#25 — Pikachu" in reply
    assert "Tipos: elétrico" in reply
    assert "Static" in reply
    assert "Lightning Rod (oculta)" in reply
    assert "HP 35" in reply
    assert "Velocidade 90" in reply
    assert "https://example.test/art.png" in reply
    assert "Pikachu Rock Star" in reply
