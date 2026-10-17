import pytest

from uva.commands import build_registry
from uva.router import Router
from uva.selfmod import codecheck
from uva.selfmod.plugins import default_plugins_dir, load_plugins, plugin_files
from uva.selfmod.testrun import run_pytest


def test_every_shipped_plugin_loads_without_errors():
    """Also guards self-coded plugins: a broken one makes this test fail."""
    registry = build_registry()
    report = load_plugins(registry, default_plugins_dir())
    assert report.errors == {}
    assert "moeda" in report.loaded
    assert registry.get("moeda").source == "plugin"
    assert registry.get("coin") is registry.get("moeda")


async def test_plugin_command_runs_through_the_router(services):
    reply = (await Router(services).handle_text("/moeda")).text
    assert reply in ("🪙 Cara!", "🪙 Coroa!")


def write(directory, name, code):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.py").write_text(code, encoding="utf-8")


PLUGIN = '''from uva.plugins.api import PluginContext, command

@command("eco", "Repete o texto")
async def echo(ctx: PluginContext) -> str:
    return ctx.text or "nada"
'''


def test_reload_adds_and_removes_plugins(tmp_path):
    registry = build_registry(tmp_path)
    assert registry.get("eco") is None
    write(tmp_path, "eco", PLUGIN)
    assert load_plugins(registry, tmp_path).loaded == {"eco": ["eco"]}
    assert registry.get("eco") is not None
    (tmp_path / "eco.py").unlink()
    load_plugins(registry, tmp_path)
    assert registry.get("eco") is None
    assert registry.get("ping") is not None  # core commands survive reloads


def test_broken_or_clashing_plugins_are_reported_not_fatal(tmp_path):
    write(tmp_path, "broken", "raise RuntimeError('boom')\n")
    write(tmp_path, "clash", PLUGIN.replace('"eco"', '"ping"'))
    write(tmp_path, "empty", "x = 1\n")
    write(tmp_path, "eco", PLUGIN)
    registry = build_registry(tmp_path)
    report = load_plugins(registry, tmp_path)
    assert set(report.errors) == {"broken", "clash", "empty"}
    assert "nome já usado" in report.errors["clash"]
    assert report.loaded == {"eco": ["eco"]}
    assert registry.get("ping").source == "core"


@pytest.mark.parametrize(
    "code",
    [
        "import os\n",
        "import subprocess\n",
        "from uva.config import get_settings\n",
        "from uva import security\n",
        "from . import api\n",
        "open('/repo/.env').read()\n",
        "eval('1')\n",
        "__import__('os')\n",
        "x = ().__class__\n",
        "getattr(object, 'x')\n",
        "import pathlib\n",
        "import importlib\n",
    ],
)
def test_static_check_rejects_dangerous_code(code):
    assert codecheck.check_plugin(code)


def test_static_check_accepts_normal_plugins():
    assert codecheck.check_plugin(PLUGIN + "import random\nimport math\n") == []
    test_code = "from uva.plugins.eco import echo\nfrom uva.plugins.testing import run\nimport pytest\n"
    assert codecheck.check_test(test_code, "eco") == []
    assert codecheck.check_test("from uva.plugins.other import x\n", "eco")


def test_name_check():
    assert codecheck.check_name("dado", {"moeda"}) == []
    assert codecheck.check_name("moeda", {"moeda"})
    assert codecheck.check_name("Dado-2", set())


def test_test_runner_reports_pass_and_fail(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    result = run_pytest(tmp_path, timeout=120)
    assert result.ok and "1 passed" in result.summary

    (tmp_path / "test_bad.py").write_text("def test_bad():\n    assert 1 == 2\n", encoding="utf-8")
    result = run_pytest(tmp_path, timeout=120)
    assert not result.ok and "1 failed" in result.summary and "assert 1 == 2" in result.output


def test_test_runner_hides_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "super-secret")
    (tmp_path / "src").mkdir()
    (tmp_path / "test_env.py").write_text(
        "import os\ndef test_env():\n    assert 'TELEGRAM_BOT_TOKEN' not in os.environ\n",
        encoding="utf-8",
    )
    assert run_pytest(tmp_path, timeout=120).ok


BILINGUAL = '''from uva.plugins.api import PluginContext, command

@command("eco", {"en": "Repeats the text", "pt": "Repete o texto"})
def echo(ctx: PluginContext) -> str:
    return ctx.lang
'''


def test_bilingual_description_follows_the_bot_language(tmp_path):
    from uva import i18n

    write(tmp_path, "eco", BILINGUAL)
    registry = build_registry(tmp_path)
    load_plugins(registry, tmp_path)
    assert registry.get("eco").description == "Repete o texto"

    i18n.set_language("en")
    load_plugins(registry, tmp_path)
    assert registry.get("eco").description == "Repeats the text"


def test_resolve_description_fallbacks():
    from uva.plugins.api import resolve_description

    assert resolve_description("Só um", "en") == "Só um"
    assert resolve_description({"en": "One", "pt": "Um"}, "pt") == "Um"
    assert resolve_description({"en": "One", "pt": "Um"}, "es") == "One"
    assert resolve_description({"pt": "Um"}, "en") == "Um"


async def test_plugin_sees_the_bot_language(services):
    from uva import i18n

    i18n.set_language("en")
    reply = (await Router(services).handle_text("/coin")).text
    assert reply in ("🪙 Heads!", "🪙 Tails!")


def test_shipped_plugins_pass_the_static_check():
    for path in plugin_files(default_plugins_dir()):
        assert codecheck.check_plugin(path.read_text(encoding="utf-8")) == [], path.name
