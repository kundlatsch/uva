import pytest
from pydantic import ValidationError

from uva.config import Settings


def test_missing_allowed_user_id_fails(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.delenv("ALLOWED_TELEGRAM_USER_ID", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_minimal_config_uses_defaults(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_ID", "42")
    settings = Settings(_env_file=None)
    assert settings.allowed_telegram_user_id == 42
    assert settings.llm_api_key is None
    assert settings.log_level == "INFO"


def test_empty_env_values_fall_back_to_defaults(monkeypatch):
    from uva.config import Settings

    monkeypatch.setenv("TRANSCRIBE_PRICE_PER_MINUTE", "")
    monkeypatch.setenv("PROJECTS_ANTHROPIC_API_KEY", "")
    s = Settings(_env_file=None, telegram_bot_token="1:a", allowed_telegram_user_id=1)
    assert s.transcribe_price_per_minute == Settings.model_fields["transcribe_price_per_minute"].default
    assert s.projects_anthropic_api_key is None
