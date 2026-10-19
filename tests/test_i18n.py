import ast
import json
import re
import string
from pathlib import Path

import pytest

from uva import i18n
from uva.config import Settings

SRC = Path(i18n.__file__).parent
REPO = SRC.parents[1]
# Two words of letters: text a person would read (not a key, a path or a code).
PROSE = re.compile(r"[A-Za-zÀ-ú]{2,}[ ,.:;!?]+[A-Za-zÀ-ú]{2,}")


def keys(language: str) -> dict[str, object]:
    return i18n.catalog(language)


def fields(value: object) -> set[str]:
    texts = value if isinstance(value, list) else [value]
    return {name for text in texts for _, name, _, _ in string.Formatter().parse(str(text)) if name}


def test_every_key_exists_in_every_language():
    en, pt = keys("en"), keys("pt")
    assert sorted(set(en) - set(pt)) == []
    assert sorted(set(pt) - set(en)) == []


def test_placeholders_match_between_languages():
    en, pt = keys("en"), keys("pt")
    mismatched = [k for k in en if k in pt and fields(en[k]) != fields(pt[k])]
    assert mismatched == []


def test_locale_files_are_valid_json():
    for path in i18n.LOCALES_DIR.glob("*/*.json"):
        json.loads(path.read_text(encoding="utf-8"))


def test_translate_and_fall_back(tmp_path, monkeypatch):
    i18n.set_language("en")
    assert i18n.t("no.such.key") == "no.such.key"
    i18n.set_language("pt")
    assert i18n.get_language() == "pt"


@pytest.mark.parametrize(("raw", "code"), [("pt-BR", "pt"), ("PT_br", "pt"), ("en", "en"), ("en-US", "en")])
def test_normalize(raw, code):
    assert i18n.normalize(raw) == code


def test_unsupported_language_is_rejected():
    with pytest.raises(ValueError):
        i18n.normalize("fr")


def test_english_is_the_default(monkeypatch):
    monkeypatch.delenv("BOT_LANGUAGE", raising=False)
    settings = Settings(_env_file=None, telegram_bot_token="1:a", allowed_telegram_user_id=1)
    assert settings.bot_language == "en"
    assert settings.phrases_file.name == "fallback_phrases.en.json"
    assert Settings(
        _env_file=None, telegram_bot_token="1:a", allowed_telegram_user_id=1, bot_language="pt-BR"
    ).phrases_file.name == "fallback_phrases.json"


def _sources():
    for path in sorted(SRC.rglob("*.py")):
        if "locales" not in path.parts:
            yield path, ast.parse(path.read_text(encoding="utf-8"))


def _call_name(node: ast.Call) -> str:
    return node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")


def test_every_key_used_in_the_code_exists():
    """t("x.y") with a literal key must find it (else the user would read the key)."""
    catalog = i18n.catalog("en")
    missing = []
    for path, tree in _sources():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _call_name(node) in ("t", "t_list") and node.args:
                key = node.args[0]
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    if key.value not in catalog and not any(k.startswith(key.value + ".") for k in catalog):
                        missing.append(f"{path.relative_to(SRC)}:{node.lineno} {key.value}")
    assert missing == []


def _literal_prose(node) -> str | None:
    """The literal text of a str constant or f-string, if it reads like prose."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        text = node.value
    elif isinstance(node, ast.JoinedStr):
        text = " ".join(v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str))
    else:
        return None
    return text if PROSE.search(text) else None


# Replies built from literals on purpose: technical text, the same in every language.
LITERAL_REPLY_ALLOWLIST: set[str] = set()


def test_no_hard_coded_text_in_replies():
    """What reaches the user (Reply, ActionResult text, Button labels, the errors
    shown in replies) goes through t(), never a literal in one language."""
    shown_errors = {"SkillError", "FileAccessError", "PlaceholderError", "ProjectError"}
    found = []
    for path, tree in _sources():
        rel = str(path.relative_to(SRC)).replace("\\", "/")
        if rel.startswith("plugins/"):
            continue  # plugins keep their own en/pt tables (see the plugin tests below)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node)
            candidates = []
            if name in ("Reply", "ActionResult", "Button") or name in shown_errors:
                candidates += node.args[:1]
                candidates += [k.value for k in node.keywords if k.arg == "text"]
            for candidate in candidates:
                if (text := _literal_prose(candidate)) and f"{rel}:{text}" not in LITERAL_REPLY_ALLOWLIST:
                    found.append(f"{rel}:{node.lineno} {text[:60]!r}")
            # Core commands: the /help usage hint comes from t() unless it is technical ("<id>").
            if name == "command":
                for keyword in node.keywords:
                    value = keyword.value
                    if keyword.arg == "usage" and isinstance(value, ast.Constant) and isinstance(value.value, str):
                        if set(re.findall(r"[A-Za-zÀ-ú]{3,}", value.value)) - TECHNICAL_USAGE_WORDS:
                            found.append(f"{rel}:{node.lineno} usage {value.value!r}")
    assert found == []


# Words that read the same in every language in a usage hint ("<url>").
TECHNICAL_USAGE_WORDS = {"url", "id"}


def _plugin_dicts(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict) and node.keys:
            keys = {k.value for k in node.keys if isinstance(k, ast.Constant)}
            if keys & {"en", "pt"}:
                yield node, keys


def test_plugin_texts_exist_in_both_languages():
    problems = []
    for path in sorted((SRC / "plugins").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node, keys in _plugin_dicts(tree):
            where = f"{path.name}:{node.lineno}"
            if not {"en", "pt"} <= keys:
                problems.append(f"{where} has only {sorted(keys & {'en', 'pt'})}")
                continue
            try:
                table = ast.literal_eval(node)
            except ValueError:
                continue
            en, pt = table["en"], table["pt"]
            if isinstance(en, dict) and isinstance(pt, dict):
                if set(en) != set(pt):
                    problems.append(f"{where} keys differ: {sorted(set(en) ^ set(pt))}")
                for key in set(en) & set(pt):
                    if fields(en[key]) != fields(pt[key]):
                        problems.append(f"{where} placeholders differ in {key}")
            elif fields(en) != fields(pt):
                problems.append(f"{where} placeholders differ")
        # @command(...): the description, and a usage with words, need both languages.
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _call_name(node) == "command":
                parts = node.args[1:2] + [k.value for k in node.keywords if k.arg in ("description", "usage")]
                for part in parts:
                    if not (isinstance(part, ast.Constant) and isinstance(part.value, str)):
                        continue
                    words = set(re.findall(r"[A-Za-zÀ-ú]{3,}", part.value)) - TECHNICAL_USAGE_WORDS
                    if words:
                        problems.append(f"{path.name}:{part.lineno} one-language text {part.value!r}")
    assert problems == []


def test_fallback_phrases_have_the_same_categories_and_placeholders():
    pt = json.loads((REPO / "data" / "fallback_phrases.json").read_text(encoding="utf-8"))["categories"]
    en = json.loads((REPO / "data" / "fallback_phrases.en.json").read_text(encoding="utf-8"))["categories"]
    assert set(pt) == set(en)
    for category in pt:
        assert {f for p in pt[category] for f in fields(p)} == {f for p in en[category] for f in fields(p)}, category


# --- stricter checks: short literals, f-strings, handler returns, turn labels ------------

# Words that read the same in every language (technical, units, names).
TECHNICAL_WORDS = {
    "url", "id", "llm", "mcp", "http", "https", "json", "api", "usd", "sha", "pid", "cpu", "ram", "ssh",
    "uva", "telegram", "openai", "anthropic", "claude", "mongo", "mongodb", "redis", "sqlite", "celery",
    "decisions", "trace", "commit", "commits", "docker", "github", "python", "bash", "markdown", "html",
}
# Calls whose first argument (or text=) is something the user reads.
SHOWN_CALLS = {"Reply", "ActionResult", "Button", "notify", "send_reply", "placeholder"}
# Calls whose argument is a turn label kept and shown later (/contexto, /historico, /parar, /log).
LABEL_CALLS = {"_remember": 0, "add_exchange": 0, "turn": 3}
# Modules whose functions build replies line by line.
REPLY_MODULES = ("commands/", "reminders.py", "projects/service.py", "selfmod/features.py", "journal.py")


def _literal_words(node) -> set[str]:
    """Words (3+ letters, not technical) in a literal or in the literal parts of an f-string."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        text = node.value
    elif isinstance(node, ast.JoinedStr):
        text = " ".join(v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str))
    else:
        return set()
    if text.startswith(("/", "data:", "uva.", "http")) or re.fullmatch(r"[\w.:/-]+", text.strip()):
        return set()  # a command, a key, a path, an identifier
    if re.match(r"\s*(SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER|PRAGMA)\b", text):
        return set()  # SQL
    return {w for w in re.findall(r"[A-Za-zÀ-ú]{3,}", text) if w.lower() not in TECHNICAL_WORDS}


def _shown_strings(tree):
    """(node, why) for literals that end up in front of the user."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if name in SHOWN_CALLS:
            yield from ((a, name) for a in node.args[:1])
            yield from ((k.value, name) for k in node.keywords if k.arg == "text")
        if name in LABEL_CALLS:
            index = LABEL_CALLS[name]
            if len(node.args) > index:
                yield node.args[index], f"{name} label"
        if name == "emit":
            yield from ((k.value, "event text") for k in node.keywords if k.arg == "text")


def test_no_hard_coded_words_in_what_the_user_sees():
    """Not even one word: f-strings like f"commit {sha}" or f"[photo] {x}" go through t()."""
    found = []
    for path, tree in _sources():
        rel = str(path.relative_to(SRC)).replace("\\", "/")
        if rel.startswith("plugins/"):
            continue
        for node, why in _shown_strings(tree):
            for candidate in ([node.body, node.orelse] if isinstance(node, ast.IfExp) else [node]):
                if words := _literal_words(candidate):
                    found.append(f"{rel}:{candidate.lineno} [{why}] {sorted(words)}")
    assert found == []


def _functions(tree):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def test_command_modules_build_replies_from_t():
    """In commands/: what a function returns, and the lines it appends, are t() texts."""
    found = []
    for path, tree in _sources():
        rel = str(path.relative_to(SRC)).replace("\\", "/")
        if not rel.startswith(REPLY_MODULES):
            continue
        for function in _functions(tree):
            for node in ast.walk(function):
                values = []
                if isinstance(node, ast.Return) and node.value is not None:
                    values = [node.value]
                elif isinstance(node, ast.Call) and _call_name(node) in ("append", "extend") and node.args:
                    values = node.args[:1]
                for value in values:
                    for candidate in ast.walk(value):
                        if isinstance(candidate, ast.Call) and _call_name(candidate) in ("t", "t_list", "say"):
                            break
                        if words := _literal_words(candidate):
                            found.append(f"{rel}:{candidate.lineno} {sorted(words)}")
                            break
    assert found == []


def test_turn_labels_are_translated():
    """The labels kept for photos, audio, buttons... exist in both languages."""
    from uva import labels

    for lang in ("en", "pt"):
        i18n.set_language(lang)
        assert labels.voice_text(labels.label("voice", text="oi")) == "oi"
    i18n.set_language("pt")
    assert labels.voice_text(i18n.t("labels.voice", lang="en", text="hi")) == "hi"  # either language
    assert labels.voice_text("plain text") is None
