import asyncio
import json
import subprocess
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fakes import FakeBusy, FakeTaskQueue, InMemoryUsageStore, with_llm
from test_stm import InMemorySessionBackend

from uva import progress
from uva.clock import utcnow
from uva.curator import refresh_idle
from uva.index.service import SearchIndex
from uva.llm.base import without_context
from uva.llm.prompts import describe_commands
from uva.replies import Reply
from uva.router import Router
from uva.selfmod.files import FileAccess
from uva.selfmod.git import GitRepo
from uva.skills import SkillLibrary
from uva.stm.service import ShortTermMemory
from uva.storage.sqlite import Database
from uva.telegram_io import bot_commands, respond
from uva.tts import Speaker, should_speak, speakable
from uva.turns import TurnTracker


def plan(reply) -> str:
    return json.dumps({"actions": [], "reply": reply})


@pytest.fixture
def qs(services, tmp_path):
    services.stm = ShortTermMemory(InMemorySessionBackend(), services.settings)
    services.tasks = FakeTaskQueue()
    services.busy = FakeBusy()
    db = Database(tmp_path / "uva.db")
    db.migrate()
    services.sqlite = db
    services.index = SearchIndex(db)
    services.turns = TurnTracker()
    return services


# --- reactions ------------------------------------------------------------------------


class FakeBot:
    def __init__(self):
        self.reactions, self.sent, self.edited = [], [], []

    async def set_message_reaction(self, chat_id, message_id, reaction):
        self.reactions.append(reaction)

    async def send_chat_action(self, **kw):
        pass

    async def send_message(self, text, **kw):
        self.sent.append(text)
        return SimpleNamespace(message_id=len(self.sent))

    async def edit_message_text(self, text, **kw):
        self.edited.append(text)


async def test_reactions_received_then_done():
    bot = FakeBot()

    async def work():
        return Reply("pong")

    await respond(bot, 1, lambda c, **k: c, work(), react_to=10)
    assert bot.reactions == ["👀", "👍"] and bot.sent == ["pong"]


async def test_reaction_on_failure_and_cleared_after_placeholder(monkeypatch):
    bot = FakeBot()

    async def failed():
        return Reply("deu ruim", failed=True)

    await respond(bot, 1, lambda c, **k: c, failed(), react_to=10)
    assert bot.reactions[-1] == "👎"

    monkeypatch.setattr(progress, "PLACEHOLDER_AFTER", 0.0)
    bot = FakeBot()

    async def slow():
        await asyncio.sleep(0.05)
        return Reply("pronto")

    await respond(bot, 1, lambda c, **k: c, slow(), react_to=10)
    assert bot.reactions == ["👀", None]  # the placeholder was the feedback


async def test_no_reactions_without_a_target():
    bot = FakeBot()

    async def work():
        return Reply("ok")

    await respond(bot, 1, lambda c, **k: c, work())
    assert bot.reactions == []


# --- /parar ---------------------------------------------------------------------------------


async def test_stop_cancels_the_running_turn(qs):
    started = asyncio.Event()

    async def slow():
        started.set()
        await asyncio.sleep(10)
        return Reply("never")

    running = asyncio.create_task(qs.turns.run(slow(), "pesquisa enorme"))
    await started.wait()
    reply = await Router(qs).handle_text("/parar")
    assert "Parei: pesquisa enorme" in reply.text
    result = await running
    assert "Parei (pesquisa enorme)" in result.text


async def test_stop_asks_which_when_several(qs):
    record = await qs.tasks.enqueue("search", {"query": "F1"}, None)
    started = asyncio.Event()

    async def slow():
        started.set()
        await asyncio.sleep(10)

    running = asyncio.create_task(qs.turns.run(slow(), "conversa"))
    await started.wait()
    router = Router(qs)
    reply = await router.handle_text("/stop")
    assert "2 coisas rodando" in reply.text
    assert [b.data for b in reply.buttons][-1] == "stop:all"
    assert f"stop:task:{record.id}" in [b.data for b in reply.buttons]
    done = await router.handle_callback("stop:all")
    assert "Parei: conversa" in done.text
    await running
    assert "Nada rodando" in (await router.handle_text("/parar")).text


def test_main_imports_and_spots_stop():
    from uva.main import is_stop

    assert is_stop("/parar") and is_stop("/stop@uvabot") and not is_stop("/status")


# --- /refazer and /desfazer_turno ------------------------------------------------------------


async def test_redo_answers_the_last_message_again(qs):
    provider, _ = with_llm(qs, plan("Paris."), plan("Paris, capital da França."))
    router = Router(qs)
    await router.handle_text("qual a capital da França?")
    reply = await router.handle_text("/refazer")
    assert reply.text == "Paris, capital da França."
    assert without_context(provider.calls[1]["messages"][-1].content) == "qual a capital da França?"


async def test_undo_turn_forgets_the_last_exchange(qs):
    with_llm(qs, plan("Anotado."))
    router = Router(qs)
    await router.handle_text("meu cachorro se chama Thor")
    reply = await router.handle_text("/desfazer_turno")
    assert "Esqueci a última troca" in reply.text and "Thor" in reply.text
    assert [t.role for t in (await qs.stm.session()).turns] == []
    assert await qs.index.turns() == [] and not await qs.index.search("thor")
    assert "Não há troca" in (await router.handle_text("/undo_turn")).text


# --- /modo ---------------------------------------------------------------------------------------


async def test_mode_overlay_goes_with_every_message(qs, tmp_path):
    qs.settings.repo_path = str(tmp_path / "repo")
    (tmp_path / "repo" / "data").mkdir(parents=True)
    (tmp_path / "repo" / "data" / "modes.json").write_text(json.dumps({"pirata": "Fale como um pirata."}),
                                                             encoding="utf-8")
    provider, _ = with_llm(qs, plan("ok"), plan("ok"), plan("ok"))
    router = Router(qs)
    assert "Modo conciso ligado" in (await router.handle_text("/modo conciso")).text
    await router.handle_text("me explica buracos negros")
    message = provider.calls[0]["messages"][-1].content
    assert "Mode for this session (conciso)" in message and "brief" in message
    first_system = provider.calls[0]["system"]
    await router.handle_text("/modo pirata")
    await router.handle_text("oi de novo, tudo certo?")
    assert "Fale como um pirata." in provider.calls[1]["messages"][-1].content
    assert provider.calls[1]["system"] == first_system  # the overlay never touches the system prompt
    await router.handle_text("/modo normal")
    await router.handle_text("e agora?")
    assert "Mode for this session" not in provider.calls[2]["messages"][-1].content
    assert "Não conheço o modo" in (await router.handle_text("/modo xyz")).text
    assert "pirata" in (await router.handle_text("/modo")).text


# --- [[como_arquivo]] -------------------------------------------------------------------------------


def test_as_file_directive():
    image = Reply("Olha a foto [[como_arquivo]]", image=b"\x89PNG", image_name="gato.png").messages()
    assert image[0].document == b"\x89PNG" and image[0].filename == "gato.png" and "[[" not in image[0].text
    text = Reply("relatório longo [[as_document]]").messages()
    assert text[0].document == "relatório longo".encode() and text[0].text == "📎 Em arquivo:"
    plain = Reply("foto", image=b"x").messages()
    assert plain[0].image == b"x" and plain[0].document is None


# --- voice replies -------------------------------------------------------------------------------------


def test_should_speak_and_speakable():
    assert not should_speak("off", True) and should_speak("voice_in", True) and not should_speak("voice_in", False)
    assert should_speak("always", False)
    assert speakable("**Oi** veja https://x.com e `code`") == "Oi veja e code"


async def test_speaker_records_the_cost(event_store):
    calls = []

    class Speech:
        async def create(self, **kw):
            calls.append(kw)
            return SimpleNamespace(content=b"OGG")

    usage = InMemoryUsageStore()
    speaker = Speaker("k", model="gpt-4o-mini-tts", voice="alloy", price_per_1k_chars=0.015, usage=usage,
                      client=SimpleNamespace(audio=SimpleNamespace(speech=Speech())))
    audio = await speaker.speak("x" * 1000)
    assert audio == b"OGG" and calls[0]["response_format"] == "opus"
    assert usage.records[0].purpose == "tts" and usage.records[0].cost_usd == pytest.approx(0.015)


# --- setMyCommands -------------------------------------------------------------------------------------


def test_bot_commands_menu(services):
    menu = bot_commands(services.registry)
    names = [c.command for c in menu]
    assert "help" in names and "parar" in names and len(menu) <= 100
    assert all(c.description for c in menu)
    services.registry.idle = {"dado"}
    assert "dado" not in [c.command for c in bot_commands(services.registry)]


# --- curator ------------------------------------------------------------------------------------------------


async def test_unused_plugins_and_skills_step_aside(qs, tmp_path):
    (tmp_path / "skills" / "old-skill").mkdir(parents=True)
    (tmp_path / "skills" / "old-skill" / "SKILL.md").write_text(
        "---\nname: old-skill\ndescription: Old one\nversion: 1\n---\n\n## When to Use\nx\n## Procedure\ny\n",
        encoding="utf-8",
    )
    qs.skills = SkillLibrary(tmp_path / "skills", qs.sqlite)
    plugins = [c.name for c in qs.registry if c.source == "plugin"]
    assert plugins
    long_ago = utcnow() - timedelta(days=40)
    assert await refresh_idle(qs, long_ago) == (set(), set())  # first seen: the clock starts then
    await Router(qs).handle_text(f"/{plugins[0]}")  # used now: not idle
    idle_plugins, idle_skills = await refresh_idle(qs)
    assert plugins[0] not in idle_plugins and set(plugins[1:]) <= idle_plugins
    assert idle_skills == {"old-skill"} and "old-skill" not in qs.skills.index_text()
    help_text = (await Router(qs).handle_text("/help")).text
    assert f"/{plugins[1]} " not in help_text and "parado" in help_text
    assert f"/{plugins[1]}" not in describe_commands(qs.registry)
    # Still callable.
    assert qs.registry.get(plugins[1]) is not None


# --- /reverter diff --------------------------------------------------------------------------------------------


def git(path, *args) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=path,
                          capture_output=True, text=True, check=True).stdout


async def test_revert_diff_shows_without_undoing(services, tmp_path):
    repo = tmp_path / "repo"
    (repo / "data").mkdir(parents=True)
    (repo / "data" / "soul.md").write_text("Sou irônica.\n", encoding="utf-8")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "data")
    git(repo, "commit", "-q", "-m", "init")
    services.files = FileAccess(repo, None, GitRepo(repo, "kundlatsch", "k@example.com"))
    services.repo = services.files.git
    await services.files.write("data/soul.md", "Sou gentil.\n", reason="teste")
    reply = await Router(services).handle_text("/reverter diff")
    assert "desfaria" in reply.text and "-Sou irônica." in reply.text and "+Sou gentil." in reply.text
    assert (repo / "data" / "soul.md").read_text(encoding="utf-8") == "Sou gentil.\n"


async def test_modes_have_english_names(qs):
    from uva import i18n

    router = Router(qs)
    assert "Modo serio ligado" in (await router.handle_text("/mode serious")).text
    assert (await qs.stm.session()).mode == "serio"
    i18n.set_language("en")
    listing = (await router.handle_text("/mode")).text
    assert "Mode now: serious" in listing and "concise" in listing and "teacher" in listing
