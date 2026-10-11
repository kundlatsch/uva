"""Application settings loaded from environment variables / .env."""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMProvider(StrEnum):
    ANTHROPIC = "anthropic"
    OPENAI_COMPATIBLE = "openai_compatible"


class Settings(BaseSettings):
    """All runtime configuration.

    Only the Telegram fields are required for now; everything else has a
    default so later phases can opt in without breaking existing setups.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Telegram (required)
    telegram_bot_token: SecretStr
    allowed_telegram_user_id: int

    # LLM
    llm_provider: LLMProvider = LLMProvider.ANTHROPIC
    llm_api_key: SecretStr | None = None
    llm_base_url: str | None = None
    llm_model_router: str | None = None
    llm_model_heavy: str | None = None
    llm_daily_budget_usd: float = 1.0
    llm_monthly_budget_usd: float = 20.0

    # Proactive behaviour
    proactive_daily_budget_usd: float = 0.5
    proactive_interval_minutes: int = 60

    # Storage
    mongo_uri: str = "mongodb://mongo:27017/uva"
    second_brain_path: str = "/data/second-brain"

    # Fallback tier 2 (similarity, 0-100): at/above "execute" the command runs,
    # at/above "confirm" the bot asks first, below goes to the LLM.
    similarity_execute_threshold: float = 90
    similarity_confirm_threshold: float = 75

    # Logging / events
    log_level: str = "INFO"
    events_ttl_days: int = 90

    # Used for "today", "this month" and displayed times.
    timezone: str = "America/Sao_Paulo"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    # pydantic fills required fields from env; mypy can't see that.
    return Settings()  # type: ignore[call-arg]
