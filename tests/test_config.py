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
