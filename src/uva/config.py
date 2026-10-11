"""Application settings loaded from environment variables / .env."""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMProvider(StrEnum):
    ANTHROPIC = "anthropic"
    OPENAI_COMPATIBLE = "openai_compatible"


# Defaults used when LLM_MODEL_ROUTER / LLM_MODEL_HEAVY are not set.
ANTHROPIC_DEFAULT_MODELS = ("claude-haiku-5-5", "claude-opus-5-5")


class SearchBackendName(StrEnum):
    AUTO = "auto"  # anthropic web search with Anthropic, otherwise wikipedia+duckduckgo
    ANTHROPIC = "anthropic"
    WIKIPEDIA = "wikipedia"
    DUCKDUCKGO = "duckduckgo"
    WIKIPEDIA_DUCKDUCKGO = "wikipedia+duckduckgo"


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
    # Only sent when set (e.g. "low" for OpenAI reasoning models).
    llm_reasoning_effort: str | None = None
    llm_max_tokens: int = 4000
    llm_timeout_seconds: float = 60
    # Recent messages kept as conversation context for the LLM.
    llm_history_messages: int = 10
    # USD per 1M tokens, JSON: {"gpt-5.5": [input, output], ...}
    llm_prices: dict[str, tuple[float, float]] = {}
    # Used for models missing from the price table (pessimistic on purpose).
    llm_default_price_input: float = 5.0
    llm_default_price_output: float = 25.0

    # Search
    search_backend: SearchBackendName = SearchBackendName.AUTO
    search_max_results: int = 5
    wikipedia_lang: str = "pt"

    # Proactive behaviour
    proactive_daily_budget_usd: float = 0.5
    # 0 disables the goal loop.
    proactive_interval_minutes: int = 60
    proactive_max_steps: int = 3  # LLM rounds per goal run
    proactive_max_tokens_per_run: int = 30000
    # Tell the owner on Telegram when background work (incl. goal runs) finishes.
    proactive_notify: bool = True

    # Storage
    mongo_uri: str = "mongodb://mongo:27017/uva"
    sqlite_path: str = "/data/sqlite/uva.db"
    second_brain_path: str = "/data/second-brain"

    # Background tasks (Celery + Redis)
    redis_url: str = "redis://redis:6379/0"
    task_max_retries: int = 3
    task_time_limit_seconds: int = 600
    tasks_ttl_days: int = 30
    # After each message the bot counts as "busy" for this long (goal loop waits).
    busy_after_message_seconds: int = 120

    # Long-term memory
    memory_working_size: int = 20  # items injected into the prompt
    memory_half_life_days: float = 30
    memory_archive_score: float = 0.05
    memory_archive_after_days: int = 90
    memory_maintenance_hour: int = 4  # local time, daily (beat)

    # Fallback tier 2 (similarity, 0-100): at/above "execute" the command runs,
    # at/above "confirm" the bot asks first, below goes to the LLM.
    similarity_execute_threshold: float = 90
    similarity_confirm_threshold: float = 75

    # Logging / events
    log_level: str = "INFO"
    events_ttl_days: int = 90

    # Repository checkout the bot may edit (soul.md, data/, plugins). Empty means
    # "the repo this code runs from"; in Docker it is mounted at /repo.
    repo_path: str = ""
    # Personality file; empty means <repo>/data/soul.md.
    soul_path: str = ""
    # Self-coding (/feature): plugins written, tested and merged by the bot.
    selfcode_daily_budget_usd: float = 2.0
    selfcode_max_attempts: int = 3
    selfcode_test_timeout_seconds: int = 300
    selfcode_time_limit_seconds: int = 1800

    # Author of the commits the bot makes in its own repository.
    git_author_name: str = "kundlatsch"
    git_author_email: str = "gustavo.kundlatsch@gmail.com"

    # Used for "today", "this month" and displayed times.
    timezone: str = "America/Sao_Paulo"

    @property
    def router_model(self) -> str | None:
        """Cheap model used for routing/planning."""
        if self.llm_model_router:
            return self.llm_model_router
        return ANTHROPIC_DEFAULT_MODELS[0] if self.llm_provider is LLMProvider.ANTHROPIC else None

    @property
    def heavy_model(self) -> str | None:
        """Stronger model used for summaries and goals."""
        if self.llm_model_heavy:
            return self.llm_model_heavy
        if self.llm_provider is LLMProvider.ANTHROPIC:
            return ANTHROPIC_DEFAULT_MODELS[1]
        return self.router_model

    @property
    def repo_dir(self) -> Path:
        if self.repo_path:
            return Path(self.repo_path)
        return Path(__file__).resolve().parents[2]  # src/uva/config.py -> repo root

    @property
    def plugins_dir(self) -> Path:
        """Plugins live in the repo (so merged ones load without a rebuild)."""
        in_repo = self.repo_dir / "src" / "uva" / "plugins"
        if in_repo.is_dir():
            return in_repo
        from uva.selfmod.plugins import default_plugins_dir

        return default_plugins_dir()

    @property
    def soul_file(self) -> Path:
        return Path(self.soul_path) if self.soul_path else self.repo_dir / "data" / "soul.md"

    @property
    def llm_default_price(self) -> tuple[float, float]:
        return (self.llm_default_price_input, self.llm_default_price_output)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    # pydantic fills required fields from env; mypy can't see that.
    return Settings()  # type: ignore[call-arg]
