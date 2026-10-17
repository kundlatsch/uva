import json
import random

import pytest
from fakes import make_settings, with_llm

from uva.llm.budget import Scope, UsageRecord
from uva.matching import small_talk
from uva.phrases import BUILTIN, Phrases, PhrasesError, placeholders, validate
from uva.router import Router

SHIPPED = make_settings().phrases_file
REQUIRED = {
    "sem_credito", "llm_erro", "nao_entendi", "ocupado", "tarefa_falhou", "tarefa_ok",
    "pesquisando", "saudacao", "agradecimento", "despedida", "confirmacao", "cancelado",
    "sem_resultado", "erro_generico",
}


@pytest.fixture
def shipped() -> dict:
    return json.loads(SHIPPED.read_text(encoding="utf-8"))


def test_shipped_file_is_valid_and_big_enough(shipped):
    categories = validate(shipped)
    assert REQUIRED <= set(categories)
    assert sum(len(v) for v in categories.values()) >= 120
    for name, items in categories.items():
        assert len(items) >= 10, name
        assert len(set(items)) == len(items), f"duplicates in {name}"


def test_english_file_matches_the_portuguese_one(shipped):
    english_file = make_settings(bot_language="en").phrases_file
    assert english_file.name == "fallback_phrases.en.json"
    english = json.loads(english_file.read_text(encoding="utf-8"))
    categories = validate(english)
    assert english["version"] == shipped["version"]
    assert set(english["placeholders"]) == set(shipped["placeholders"])
    assert set(categories) == set(shipped["categories"])
    for name, items in categories.items():
        pt_items = shipped["categories"][name]
        assert len(items) == len(pt_items), name
        assert len(set(items)) == len(items), f"duplicates in {name}"
        en_names = set().union(*(placeholders(item) for item in items))
        pt_names = set().union(*(placeholders(item) for item in pt_items))
        assert en_names == pt_names, name


def test_every_placeholder_is_documented_and_renders(shipped):
    phrases = Phrases(SHIPPED)
    values = {key: "X" for key in shipped["placeholders"]}
    for name, items in shipped["categories"].items():
        for item in items:
            assert placeholders(item) <= set(shipped["placeholders"])
            assert "{" not in item.format(**values)


def test_phrases_needing_missing_values_are_skipped():
    phrases = Phrases(SHIPPED)
    for _ in range(50):
        assert "{" not in phrases.pick("tarefa_falhou")  # no id given
    assert any("abc123" in phrases.pick("tarefa_falhou", id="abc123") for _ in range(50))


def test_no_repeats_within_recent_window(tmp_path):
    path = tmp_path / "f.json"
    path.write_text(json.dumps({"version": 1, "categories": {"c": ["a", "b", "c", "d", "e"]}}), encoding="utf-8")
    phrases = Phrases(path, rng=random.Random(0))
    picks = [phrases.pick("c") for _ in range(40)]
    for i in range(len(picks) - 3):
        window = picks[i : i + 4]
        assert len(set(window)) == 4  # never one of the last 3


def test_tiny_category_still_works(tmp_path):
    path = tmp_path / "f.json"
    path.write_text(json.dumps({"version": 1, "categories": {"c": ["só esta"]}}), encoding="utf-8")
    phrases = Phrases(path)
    assert [phrases.pick("c") for _ in range(3)] == ["só esta"] * 3


def test_missing_or_invalid_file_falls_back(tmp_path):
    missing = Phrases(tmp_path / "nope.json")
    assert missing.pick("nao_entendi") == BUILTIN["nao_entendi"]
    assert missing.pick("categoria_que_nao_existe") == BUILTIN["erro_generico"]

    bad = tmp_path / "bad.json"
    bad.write_text("{ isso não é json", encoding="utf-8")
    assert Phrases(bad).pick("saudacao") == BUILTIN["saudacao"]


def test_reload_on_change_keeps_last_good_version(tmp_path):
    path = tmp_path / "f.json"
    path.write_text(json.dumps({"version": 1, "categories": {"c": ["um"]}}), encoding="utf-8")
    phrases = Phrases(path)
    assert phrases.pick("c") == "um"
    import os

    path.write_text(json.dumps({"version": 1, "categories": {"c": ["dois"]}}), encoding="utf-8")
    os.utime(path, (path.stat().st_atime, path.stat().st_mtime + 5))
    assert phrases.pick("c") == "dois"
    path.write_text("quebrado", encoding="utf-8")
    os.utime(path, (path.stat().st_atime, path.stat().st_mtime + 10))
    assert phrases.pick("c") == "dois"


@pytest.mark.parametrize(
    "data",
    [
        {"categories": {"c": ["x"]}},
        {"version": 1, "categories": {}},
        {"version": 1, "categories": {"c": []}},
        {"version": 1, "categories": {"c": [""]}},
        {"version": 1, "categories": {"c": ["oi {nome}"]}},
        {"version": 1, "categories": {"c": ["x" * 400]}},
    ],
)
def test_validation_rejects_bad_files(data):
    with pytest.raises(PhrasesError):
        validate(data)


@pytest.mark.parametrize(
    ("text", "category"),
    [
        ("oi", "saudacao"), ("Olá!", "saudacao"), ("bom dia", "saudacao"), ("e aí", "saudacao"),
        ("valeu", "agradecimento"), ("valeuuu", "agradecimento"), ("obrigado!", "agradecimento"),
        ("falou", "despedida"), ("tchau", "despedida"),
        ("oi, pesquisa sobre F1 pra mim", None), ("qual a capital da França", None), ("ping", None),
    ],
)
def test_small_talk_detection(text, category):
    assert small_talk(text) == category


async def test_greetings_are_answered_without_the_llm(services):
    provider, _ = with_llm(services)
    reply = await Router(services).handle_text("valeu!")
    assert reply.text in services.phrases.categories["agradecimento"]
    assert provider.calls == []


async def test_budget_exceeded_uses_phrase_plus_facts(services):
    provider, usage = with_llm(services)
    usage.records.append(
        UsageRecord(
            provider="p", model="m", input_tokens=0, output_tokens=0, purpose="t",
            cost_usd=services.settings.llm_monthly_budget_usd, scope=Scope.INTERACTIVE,
        )
    )
    reply = await Router(services).handle_text("me conta uma piada")
    phrase, facts = reply.text.split("\n")
    assert phrase in services.phrases.categories["sem_credito"]
    assert "orçamento mensal" in facts and "próximo mês" in facts
    assert provider.calls == []


async def test_quota_error_from_provider_uses_sem_credito(services):
    from uva.llm.base import LLMError

    with_llm(services)

    async def broke(*args, **kwargs):
        raise LLMError("openai: Error code: 429 - insufficient_quota")

    services.llm.provider.complete = broke
    reply = await Router(services).handle_text("me conta uma piada")
    assert reply.text.split("\n")[0] in services.phrases.categories["sem_credito"]


async def test_frases_command(services):
    router = Router(services)
    overview = (await router.handle_text("/frases")).text
    assert "sem_credito:" in overview and "categorias" in overview
    sample = (await router.handle_text("/frases saudacao")).text
    assert sample.startswith("saudacao (")
    assert "desconhecida" in (await router.handle_text("/frases xyz")).text


async def test_phrase_file_edits_are_validated(tmp_path):
    from uva.selfmod.files import FileAccess, FileAccessError

    repo = tmp_path / "repo"
    (repo / "data").mkdir(parents=True)
    files = FileAccess(repo, None, None)
    with pytest.raises(FileAccessError):
        await files.write("data/fallback_phrases.json", "{quebrado", reason="")
    with pytest.raises(FileAccessError):
        await files.write("data/fallback_phrases.json", json.dumps({"version": 1, "categories": {}}), reason="")
    with pytest.raises(FileAccessError):
        await files.append("data/fallback_phrases.json", "x", reason="")
    ok = json.dumps({"version": 1, "categories": {"saudacao": ["E aí!"]}})
    await files.write("data/fallback_phrases.json", ok, reason="")
    with pytest.raises(FileAccessError):
        files.resolve("brain/x.json")  # .json only inside data/


def test_builtin_phrases_and_errors_follow_the_language(tmp_path):
    from uva import i18n

    missing = Phrases(tmp_path / "nope.json")
    assert missing.pick("saudacao") == "Olá! Em que posso ajudar?"
    i18n.set_language("en")
    assert missing.pick("saudacao") == "Hi! How can I help?"
    assert missing.pick("comando_desconhecido", comando="xyz").startswith("/xyz doesn't exist")
    assert set(BUILTIN) >= set(REQUIRED)
    with pytest.raises(PhrasesError, match="must be a non-empty list"):
        validate({"version": 1, "categories": {"c": []}})


@pytest.mark.parametrize(
    ("text", "category"),
    [
        ("hi", "saudacao"), ("Hello!", "saudacao"), ("good morning", "saudacao"),
        ("thanks!", "agradecimento"), ("thank you", "agradecimento"),
        ("bye", "despedida"), ("see you later", "despedida"),
        ("hi, search F1 for me please", None), ("what is the capital of France", None),
    ],
)
def test_small_talk_detection_in_english(text, category):
    assert small_talk(text) == category
