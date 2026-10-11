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

    reply = await Router(services).handle_text("oi")
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
    assert "parceir" in text and "Precisão" in text
