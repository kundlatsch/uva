import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fakes import FakeBusy, with_llm

from uva.llm.budget import Scope, UsageRecord
from uva.logging.events import EventType
from uva.router import Router
from uva.selfmod.features import FeatureRecord, FeatureService, FeatureStatus
from uva.selfmod.git import SELF_CODE, GitRepo
from uva.selfmod.selfcode import build_feature
from uva.selfmod.testrun import SuiteResult

REPO_ROOT = Path(__file__).resolve().parents[1]

DICE = '''import random

from uva.plugins.api import PluginContext, command


@command("dado", "Rola um dado", usage="[lados]", examples=("rola um dado",))
def roll(ctx: PluginContext) -> str:
    sides = int(ctx.args[0]) if ctx.args and ctx.args[0].isdigit() else 6
    return f"🎲 {random.randint(1, sides)}"
'''
DICE_TEST = '''from uva.plugins.dado import roll
from uva.plugins.testing import run


def test_roll_in_range():
    value = int(run(roll).split()[-1])
    assert 1 <= value <= 6
'''


def generated(**overrides) -> str:
    data = {
        "name": "dado",
        "command": "dado",
        "summary": "Rola um dado de N lados.",
        "usage": "/dado 20",
        "plugin_code": DICE,
        "test_code": DICE_TEST,
    }
    data.update(overrides)
    return json.dumps(data)


def git(path, *args) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=path, capture_output=True, text=True, check=True,
    ).stdout


class InMemoryFeatureStore:
    def __init__(self) -> None:
        self.records: dict[str, FeatureRecord] = {}

    async def add(self, record):
        self.records[record.id] = record

    async def get(self, feature_id):
        return self.records.get(feature_id)

    async def update(self, feature_id, **fields):
        self.records[feature_id] = self.records[feature_id].model_copy(update=fields)

    async def recent(self, limit):
        return list(self.records.values())[-limit:]


class Inbox:
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def __call__(self, reply) -> None:
        self.texts.append(reply.text)


@pytest.fixture
def repo(tmp_path) -> Path:
    """A small repository with the plugin interface and the example plugin."""
    root = tmp_path / "repo"
    for rel in ("src/uva/plugins/api.py", "src/uva/plugins/moeda.py", "tests/plugins/test_moeda.py"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO_ROOT / rel, root / rel)
    git(root, "init", "-q", "-b", "main")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "init")
    return root


@pytest.fixture
def setup(services, repo):
    services.settings.repo_path = str(repo)
    services.repo = GitRepo(repo, "kundlatsch", "k@example.com")
    services.busy = FakeBusy()
    enqueued: list[str] = []

    async def enqueue(feature_id):
        enqueued.append(feature_id)

    services.features = FeatureService(InMemoryFeatureStore(), enqueue)
    return enqueued


def runner(*results: bool):
    outcomes = list(results)
    calls: list[Path] = []

    async def run(work: Path) -> SuiteResult:
        calls.append(work)
        ok = outcomes.pop(0)
        return SuiteResult(ok, "170 passed" if ok else "1 failed", "" if ok else "AssertionError: x")

    run.calls = calls
    return run


async def reload_ok():
    return {"loaded": {"dado": ["dado"]}, "errors": {}}


async def new_feature(services, description="/dado rola um dado de 6 lados") -> FeatureRecord:
    return await services.features.request(description, None)


async def test_feature_command_enqueues(services, setup):
    with_llm(services)
    reply = (await Router(services).handle_text("/feature /dado rola um dado de 6 lados")).text
    assert reply.startswith("🛠️ Beleza") and len(setup) == 1
    listing = (await Router(services).handle_text("/features")).text
    assert setup[0] in listing and "na fila" in listing


async def test_green_feature_is_committed_merged_and_announced(services, setup, repo, event_store):
    provider, _ = with_llm(services, generated())
    record = await new_feature(services)
    inbox = Inbox()

    final = await build_feature(services, record.id, inbox, request_reload=reload_ok, run_tests=runner(True))

    assert final.status is FeatureStatus.MERGED and final.attempts == 1
    assert (repo / "src/uva/plugins/dado.py").read_text(encoding="utf-8") == DICE
    assert (repo / "tests/plugins/test_dado.py").exists()
    message = git(repo, "log", "-1", "--format=%an|%B")
    assert message.startswith("kundlatsch|Self-code: /dado (dado)")
    assert f"{SELF_CODE}: true" in message and f"UVA-Feature-Id: {record.id}" in message
    assert git(repo, "branch", "--list", "uva/*") == ""  # feature branch cleaned up
    assert inbox.texts[0].startswith("🛠️ Pronto! Criei /dado")
    assert "Uso: /dado 20" in inbox.texts[0] and "170 passed" in inbox.texts[0]
    # Code generation runs without the personality and on the self-code budget.
    assert provider.calls[0]["model"] == "heavy-model"
    assert "Interface de plugins" in provider.calls[0]["system"]
    assert services.busy.markers == {}
    assert {e.data["op"] for e in event_store.of_type(EventType.SELFCODE)} >= {"start", "attempt", "finish"}


async def test_failing_tests_are_fed_back_and_retried(services, setup, repo):
    provider, _ = with_llm(services, generated(), generated())
    record = await new_feature(services)
    final = await build_feature(
        services, record.id, Inbox(), request_reload=reload_ok, run_tests=runner(False, True)
    )
    assert final.status is FeatureStatus.MERGED and final.attempts == 2
    assert "AssertionError: x" in provider.calls[1]["messages"][0].content


async def test_dangerous_code_never_runs(services, setup, repo):
    evil = DICE.replace("import random", "import os\nimport random")
    provider, _ = with_llm(services, *(generated(plugin_code=evil) for _ in range(3)))
    run = runner()
    record = await new_feature(services)
    inbox = Inbox()
    final = await build_feature(services, record.id, inbox, request_reload=reload_ok, run_tests=run)
    assert final.status is FeatureStatus.FAILED and final.attempts == 3
    assert run.calls == []  # tests (i.e. the code) never ran
    assert "import proibido: os" in provider.calls[1]["messages"][0].content
    assert not (repo / "src/uva/plugins/dado.py").exists()
    assert inbox.texts[0].startswith("😵 Não consegui construir")


async def test_existing_command_name_is_refused(services, setup):
    with_llm(services, *(generated(name="pingx", command="ping") for _ in range(3)))
    record = await new_feature(services)
    final = await build_feature(services, record.id, Inbox(), request_reload=reload_ok, run_tests=runner())
    assert final.status is FeatureStatus.FAILED and "/ping já existe" in final.error


async def test_plugin_that_fails_to_load_in_the_bot_is_reverted(services, setup, repo):
    with_llm(services, generated())
    reloads = []

    async def reload_broken():
        reloads.append(1)
        return {"loaded": {}, "errors": {"dado": "ImportError: boom"}}

    record = await new_feature(services)
    final = await build_feature(services, record.id, Inbox(), request_reload=reload_broken, run_tests=runner(True))
    assert final.status is FeatureStatus.REVERTED and "boom" in final.error
    assert not (repo / "src/uva/plugins/dado.py").exists()
    assert git(repo, "log", "-1", "--format=%s").startswith('Revert "Self-code: /dado')
    assert len(reloads) == 2  # once to load, once after the revert


async def test_selfcode_budget_blocks(services, setup):
    _, usage = with_llm(services)
    usage.records.append(
        UsageRecord(
            provider="p", model="m", input_tokens=0, output_tokens=0, purpose="selfcode",
            cost_usd=services.settings.selfcode_daily_budget_usd, scope=Scope.SELFCODE,
        )
    )
    reply = (await Router(services).handle_text("/feature /dado rola um dado")).text
    assert "orçamento diário de auto-código" in reply
    # Self-coding spend doesn't eat the general daily budget.
    assert (await services.budget.check(Scope.INTERACTIVE)).allowed


async def test_reverter_undoes_a_self_coded_feature(services, setup, repo):
    with_llm(services, generated())
    record = await new_feature(services)
    await build_feature(services, record.id, Inbox(), request_reload=reload_ok, run_tests=runner(True))
    reloaded = []

    async def hook():
        reloaded.append(1)

    services.on_code_changed.append(hook)
    reply = (await Router(services).handle_text("/reverter")).text
    assert reply.startswith("↩️ Revertido") and "Self-code: /dado" in reply
    assert not (repo / "src/uva/plugins/dado.py").exists()
    assert services.features.store.records[record.id].status is FeatureStatus.REVERTED
    assert reloaded == [1]


async def test_feature_of_a_goal_completes_it(services, setup, repo):
    from fakes import InMemoryGoalStore

    from uva.goals.store import Goal, GoalStatus

    services.goals = InMemoryGoalStore()
    await services.goals.add(Goal(id="g9", title="Ter um dado"))
    with_llm(services, generated())
    record = await services.features.request("/dado rola um dado", None, goal_id="g9")
    inbox = Inbox()
    await build_feature(services, record.id, inbox, request_reload=reload_ok, run_tests=runner(True))
    assert services.goals.goals["g9"].status is GoalStatus.CONCLUIDO
    assert 'Objetivo "Ter um dado" concluído ✅' in inbox.texts[0]


def test_selfcode_report_in_english():
    from uva import i18n
    from uva.selfmod.features import describe_feature
    from uva.selfmod.selfcode import _announcement

    i18n.set_language("en")
    record = FeatureRecord(
        id="f1", description="a die", status=FeatureStatus.MERGED, command="dado", summary="Rolls a die",
        usage="/dado 20", tests="3 passed", attempts=1, commit="abcdef123", cost_usd=0.01,
    )
    assert _announcement(record) == (
        "🛠️ Done! I made /dado: Rolls a die\nUsage: /dado 20\n"
        "Tests: 3 passed · attempts: 1 · cost ≈ US$ 0.010\nCommit abcdef1 on main (/undo reverts it)."
    )
    failed = record.model_copy(update={"status": FeatureStatus.FAILED, "error": "boom"})
    assert _announcement(failed).endswith("(1 attempt(s), cost ≈ US$ 0.010).\nReason: boom")
    assert describe_feature(failed).startswith("f1 ❌ failed /dado")
