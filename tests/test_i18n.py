import json
import string

import pytest

from uva import i18n
from uva.config import Settings


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
