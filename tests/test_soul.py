import json
import os

from fakes import with_llm

from uva.router import Router
from uva.soul import PREAMBLE, Soul


def test_soul_reloads_when_file_changes(tmp_path):
    path = tmp_path / "soul.md"
    soul = Soul(path)
    assert soul.text() == "" and soul.apply("sys") == "sys"

    path.write_text("Sou irônica.", encoding="utf-8")
    assert soul.apply("sys") == f"{PREAMBLE}Sou irônica.\n\n---\n\nsys"

    path.write_text("Sou gentil.", encoding="utf-8")
    stat = path.stat()
    os.utime(path, (stat.st_atime, stat.st_mtime + 5))  # make sure mtime moves
    assert soul.text() == "Sou gentil."


async def test_every_llm_call_gets_the_soul_except_opt_out(services, tmp_path):
    path = tmp_path / "soul.md"
    path.write_text("Fala palavrão.", encoding="utf-8")
    provider, _ = with_llm(services, json.dumps({"actions": [], "reply": "Beleza, porra."}))
    services.llm.soul = Soul(path)

    reply = await Router(services).handle_text("me conta uma piada")
    assert reply.text == "Beleza, porra."
    assert provider.calls[0]["system"].startswith(PREAMBLE + "Fala palavrão.")

    provider.replies.append("x")
    from uva.llm.base import LLMMessage
    from uva.llm.budget import Scope

    await services.llm.complete([LLMMessage("user", "x")], purpose="t", scope=Scope.INTERACTIVE, system="code", use_soul=False)
    assert provider.calls[1]["system"] == "code"


async def test_soul_command(services, tmp_path):
    path = tmp_path / "soul.md"
    path.write_text("# Alma\nIrônica.", encoding="utf-8")
    services.settings.soul_path = str(path)
    assert "Irônica." in (await Router(services).handle_text("/soul")).text


def test_repository_ships_a_soul():
    from uva.config import Settings

    settings = Settings(_env_file=None, telegram_bot_token="x", allowed_telegram_user_id=1)
    text = settings.soul_file.read_text(encoding="utf-8")
    assert "partner" in text and "Precision" in text
    # Replies follow the language the system prompt asks for, not the soul's.
    assert "language the system prompt asks for" in text



def test_profile_and_soul_come_first_and_profile_before_soul(tmp_path):
    from uva.soul import Soul

    (tmp_path / "soul.md").write_text("Sarcástica.", encoding="utf-8")
    (tmp_path / "profile.md").write_text("# Perfil\nGosta de boxe.", encoding="utf-8")
    soul = Soul(tmp_path / "soul.md", profile=tmp_path / "profile.md")
    system = soul.apply("REGRAS DINAMICAS")
    assert system.index("Gosta de boxe.") < system.index("Sarcástica.") < system.index("REGRAS DINAMICAS")
    # Stable prefix: the same documents give the same start, whatever comes after.
    other = soul.apply("outra coisa")
    prefix = system[: system.index("REGRAS")]
    assert other.startswith(prefix)
    # Missing profile: just the soul.
    assert "Perfil" not in Soul(tmp_path / "soul.md", profile=tmp_path / "nope.md").apply("x")


def test_cached_input_is_cheaper():
    from uva.llm.pricing import CACHED_INPUT_FACTOR, PriceTable

    table = PriceTable({"m": (1.0, 0.0)}, default=(1.0, 0.0))
    full = table.cost("m", 10_000, 0)
    cached = table.cost("m", 10_000, 0, cached_input_tokens=8_000)
    assert cached == (2_000 + 8_000 * CACHED_INPUT_FACTOR) / 1_000_000 and cached < full
