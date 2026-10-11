from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fakes import InMemoryGoalStore, InMemoryMemoryStore, make_settings

from uva.brain.vault import Vault
from uva.goals.store import Goal, GoalStatus
from uva.memory import MemoryService
from uva.router import Router
from uva.selfmod import codecheck
from uva.selfmod.plugin_data import make_data_view
from uva.selfmod.plugins import load_plugins
from uva.storage.expenses import ExpenseRepository
from uva.storage.sqlite import Database

METHODS = {
    "working_memory", "memories", "memory", "goals", "goal", "recent_refs",
    "expenses_summary", "recent_notes",
}


@pytest.fixture
async def data_services(services, tmp_path):
    services.memory = MemoryService(InMemoryMemoryStore(), make_settings())
    await services.memory.save("cidade", "Florianópolis", relevance=0.9)
    services.goals = InMemoryGoalStore()
    await services.goals.add(Goal(id="g1", title="Ler mais", next_run_at=datetime(2026, 10, 11, 17, 38, tzinfo=UTC)))
    await services.goals.add(Goal(id="g2", title="Pausado", status=GoalStatus.PAUSADO))
    db = Database(tmp_path / "x.db")
    db.migrate()
    services.expenses = ExpenseRepository(db)
    await services.expenses.add(2590, "mercado")
    services.vault = Vault(tmp_path / "brain", ZoneInfo("America/Sao_Paulo"))
    services.vault.ensure_structure()
    services.vault.create_note("Ideia", "texto")
    return services


def test_view_exposes_only_read_methods(services):
    view = make_data_view(services)
    public = {name for name in dir(view) if not name.startswith("_")}
    assert public == METHODS
    with pytest.raises(AttributeError):
        view.services = services  # no attributes can be attached
    assert not any(name.startswith(("set", "save", "add", "delete", "update")) for name in public)


async def test_view_returns_plain_local_data(data_services):
    view = make_data_view(data_services)
    assert await view.working_memory() == [{"key": "cidade", "value": "Florianópolis", "category": "fato"}]
    assert (await view.memory("cidade"))["value"] == "Florianópolis"
    active = await view.goals(status="ativo")
    assert [g["id"] for g in active] == ["g1"]
    assert active[0]["next_run"] == "11/10 14:38"  # local time
    assert active[0]["last_attempt"] == "nunca"
    summary = await view.expenses_summary("mes")
    assert summary["total"] == "R$ 25,90" and summary["by_category"][0]["category"] == "mercado"
    notes = await view.recent_notes()
    assert notes[0]["title"] == "Ideia" and notes[0]["folder"] == "00-inbox"

    # Returned values are copies: changing them changes nothing.
    active[0]["title"] = "hackeado"
    assert (await view.goal("g1"))["title"] == "Ler mais"


async def test_view_without_services_is_empty(services):
    view = make_data_view(services)
    assert await view.goals() == [] and await view.recent_refs() == []
    assert (await view.expenses_summary())["total"] == "R$ 0,00"


def test_static_check_blocks_reaching_behind_the_view():
    code = "async def f(ctx):\n    return ctx.data.goals.__closure__\n"
    assert codecheck.check_plugin(code)
    code = "async def f(ctx):\n    return await ctx.data.goals()\n"
    assert codecheck.check_plugin(code) == []


PLUGIN = '''from uva.plugins.api import PluginContext, command


@command("metas_teste", "teste")
async def goals(ctx: PluginContext) -> str:
    items = await ctx.data.goals(status="ativo")
    return ", ".join(g["title"] for g in items) or "nenhum"
'''


async def test_plugins_get_real_data_through_the_router(data_services, tmp_path):
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    (plugins / "metas_teste.py").write_text(PLUGIN, encoding="utf-8")
    assert load_plugins(data_services.registry, plugins).ok
    assert (await Router(data_services).handle_text("/metas_teste")).text == "Ler mais"


def test_static_data_helper():
    from uva.plugins.testing import StaticData, run

    async def handler(ctx):
        return str(len(await ctx.data.goals(status="ativo")))

    data = StaticData(goals=[{"id": "1", "status": "ativo"}, {"id": "2", "status": "pausado"}])
    assert run(handler, data=data) == "1"
