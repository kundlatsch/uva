"""Consulta Pokémon na PokeAPI, incluindo formas regionais e alternativas."""

from __future__ import annotations

from typing import Any
import re
import unicodedata

import httpx

from uva.plugins.api import PluginContext, command


POKEAPI_BASE = "https://pokeapi.co/api/v2"
MAX_VARIETIES_SHOWN = 10

STAT_NAMES = {
    "hp": "HP",
    "attack": "Ataque",
    "defense": "Defesa",
    "special-attack": "Atq. esp.",
    "special-defense": "Def. esp.",
    "speed": "Velocidade",
}

TYPE_NAMES = {
    "normal": "normal",
    "fire": "fogo",
    "water": "água",
    "electric": "elétrico",
    "grass": "planta",
    "ice": "gelo",
    "fighting": "lutador",
    "poison": "venenoso",
    "ground": "terra",
    "flying": "voador",
    "psychic": "psíquico",
    "bug": "inseto",
    "rock": "pedra",
    "ghost": "fantasma",
    "dragon": "dragão",
    "dark": "sombrio",
    "steel": "aço",
    "fairy": "fada",
}

USAGE = "Uso: /pokemon <nome|id> [forma]. Ex.: /pokemon pikachu, /pokemon 150, /pokemon ninetales alola."


class PokemonApiError(Exception):
    """Erro ao consultar a PokeAPI."""


def normalize_term(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text.strip().lower())
    without_accents = "".join(char for char in normalized if not unicodedata.combining(char))
    without_accents = without_accents.replace("♀", "-f").replace("♂", "-m")
    slug = re.sub(r"[^a-z0-9]+", "-", without_accents)
    return slug.strip("-")


def build_target(args: list[str]) -> str | None:
    if not args:
        return None

    parts = [normalize_term(arg) for arg in args if normalize_term(arg)]
    if not parts:
        return None

    if len(parts) > 4:
        return ""

    if parts[0].isdigit():
        if len(parts) > 1:
            return ""
        number = int(parts[0])
        if number <= 0:
            return ""
        return str(number)

    return "-".join(parts)


def display_name(name: str) -> str:
    replacements = {
        "alola": "Alola",
        "galar": "Galar",
        "hisui": "Hisui",
        "paldea": "Paldea",
        "mega": "Mega",
        "gmax": "Gigantamax",
    }
    words = []
    for part in name.split("-"):
        words.append(replacements.get(part, part.capitalize()))
    return " ".join(words)


def type_name(name: str) -> str:
    return TYPE_NAMES.get(name, name)


def stat_name(name: str) -> str:
    return STAT_NAMES.get(name, name.replace("-", " "))


def extract_sprite(data: dict[str, Any]) -> str:
    sprites = data.get("sprites") or {}
    other = sprites.get("other") or {}
    artwork = other.get("official-artwork") or {}
    return artwork.get("front_default") or sprites.get("front_default") or "não disponível"


def extract_types(data: dict[str, Any]) -> list[str]:
    entries = sorted(data.get("types") or [], key=lambda item: item.get("slot", 0))
    names = []
    for entry in entries:
        type_data = entry.get("type") or {}
        name = type_data.get("name")
        if name:
            names.append(type_name(name))
    return names


def extract_abilities(data: dict[str, Any]) -> list[str]:
    entries = sorted(data.get("abilities") or [], key=lambda item: item.get("slot", 0))
    names = []
    for entry in entries:
        ability = entry.get("ability") or {}
        name = ability.get("name")
        if name:
            label = display_name(name)
            if entry.get("is_hidden"):
                label = f"{label} (oculta)"
            names.append(label)
    return names


def extract_stats(data: dict[str, Any]) -> list[str]:
    stats = []
    for entry in data.get("stats") or []:
        stat = entry.get("stat") or {}
        name = stat.get("name")
        value = entry.get("base_stat")
        if name and value is not None:
            stats.append(f"{stat_name(name)} {value}")
    return stats


def format_varieties(varieties: list[str], current_name: str) -> str:
    unique = []
    for name in varieties:
        if name not in unique:
            unique.append(name)

    alternatives = [name for name in unique if name != current_name]
    if not alternatives:
        return "Formas/variações: nenhuma variação listada."

    shown = alternatives[:MAX_VARIETIES_SHOWN]
    labels = ", ".join(display_name(name) for name in shown)
    remaining = len(alternatives) - len(shown)
    if remaining > 0:
        labels = f"{labels} e mais {remaining}"
    return f"Formas/variações: {labels}."


def format_pokemon(data: dict[str, Any], varieties: list[str]) -> str:
    name = data.get("name") or "desconhecido"
    dex_id = data.get("id") or "?"
    height = data.get("height")
    weight = data.get("weight")

    types = extract_types(data)
    abilities = extract_abilities(data)
    stats = extract_stats(data)
    sprite = extract_sprite(data)

    height_text = f"{height / 10:g} m" if isinstance(height, int) else "?"
    weight_text = f"{weight / 10:g} kg" if isinstance(weight, int) else "?"

    lines = [
        f"#{dex_id} — {display_name(name)}",
        f"Tipos: {', '.join(types) if types else 'não informado'}",
        f"Habilidades: {', '.join(abilities) if abilities else 'não informado'}",
        f"Stats base: {', '.join(stats) if stats else 'não informado'}",
        f"Altura/peso: {height_text} / {weight_text}",
        f"Sprite: {sprite}",
        format_varieties(varieties, name),
    ]
    return "\n".join(lines)


async def fetch_pokemon(target: str) -> tuple[dict[str, Any] | None, list[str]]:
    timeout = httpx.Timeout(8.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        response = await client.get(f"{POKEAPI_BASE}/pokemon/{target}")
        if response.status_code == 404:
            return None, []
        if response.status_code >= 400:
            raise PokemonApiError("PokeAPI retornou erro.")

        data = response.json()
        varieties: list[str] = []
        species = data.get("species") or {}
        species_url = species.get("url")
        if species_url:
            species_response = await client.get(species_url)
            if species_response.status_code < 400:
                species_data = species_response.json()
                for item in species_data.get("varieties") or []:
                    pokemon_data = item.get("pokemon") or {}
                    variety_name = pokemon_data.get("name")
                    if variety_name:
                        varieties.append(variety_name)

        if not varieties:
            varieties = [data.get("name") or target]
        return data, varieties


@command(
    "pokemon",
    "Busca dados de um Pokémon na PokeAPI, incluindo formas e variações",
    aliases=("poke", "pokedex"),
    usage="<nome|id> [forma]",
    examples=("/pokemon pikachu", "/pokemon mewtwo", "/pokemon ninetales alola", "/pokemon 25"),
)
async def pokemon(ctx: PluginContext) -> str:
    target = build_target(ctx.args)
    if target is None or target == "":
        return USAGE

    try:
        data, varieties = await fetch_pokemon(target)
    except httpx.HTTPError:
        return "Não consegui consultar a PokeAPI agora. Tente novamente em instantes."
    except PokemonApiError:
        return "A PokeAPI respondeu com erro. Tente novamente em instantes."

    if data is None:
        return (
            f"Não encontrei esse Pokémon ou forma: {target}.\n"
            "Dica: para formas regionais, tente algo como /pokemon ninetales alola, "
            "/pokemon meowth galar ou /pokemon mewtwo mega x."
        )

    return format_pokemon(data, varieties)
