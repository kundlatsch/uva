"""Application settings loaded from environment variables / .env."""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, field_validator
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
        # "KEY=" in .env means "not set" (use the default), not an empty value.
        env_ignore_empty=True,
    )

    # Telegram (required)
    telegram_bot_token: SecretStr
    allowed_telegram_user_id: int

    # Language of everything the user reads (replies, help, notices): "en" or "pt".
    bot_language: str = "en"

    # LLM
    llm_provider: LLMProvider = LLMProvider.ANTHROPIC
    llm_api_key: SecretStr | None = None
    llm_base_url: str | None = None
    llm_model_router: str | None = None
    llm_model_heavy: str | None = None
    # Model for messages with images; empty = the router model.
    llm_model_vision: str | None = None
    llm_daily_budget_usd: float = 1.0
    llm_monthly_budget_usd: float = 20.0
    # Only sent when set (e.g. "low" for OpenAI reasoning models).
    llm_reasoning_effort: str | None = None
    llm_max_tokens: int = 4000
    llm_timeout_seconds: float = 60
    # Retries on 429/5xx/timeouts (exponential backoff, honours Retry-After).
    llm_max_retries: int = 3
    # Optional second provider, used when the first still fails after retries.
    llm_fallback_provider: LLMProvider | None = None
    llm_fallback_model: str | None = None
    llm_fallback_base_url: str | None = None
    llm_fallback_api_key: SecretStr | None = None
    # A routine or goal that failed because the LLM was unreachable is retried this often.
    llm_retry_minutes: int = 10
    # Recent messages kept as conversation context for the LLM.
    llm_history_messages: int = 10
    # USD per 1M tokens, JSON: {"gpt-5.5": [input, output], ...}
    llm_prices: dict[str, tuple[float, float]] = {}
    # Used for models missing from the price table (pessimistic on purpose).
    llm_default_price_input: float = 5.0
    llm_default_price_output: float = 25.0

    # OpenAI Decisions API (typed answers). Key defaults to LLM_API_KEY when the
    # main provider is OpenAI itself.
    decisions_api_key: SecretStr | None = None
    decisions_base_url: str | None = None
    decisions_model: str = "gpt-6-luna"
    decisions_price_input: float = 0.10  # USD per 1M input tokens (output is free)

    # Speech to text (OpenAI). Key defaults to LLM_API_KEY when the provider is OpenAI.
    transcribe_api_key: SecretStr | None = None
    transcribe_base_url: str | None = None
    transcribe_model: str = "gpt-4o-mini-transcribe"
    transcribe_fallback_model: str = "whisper-1"
    transcribe_language: str = "pt"
    # USD per audio minute; empty = the built-in table for the model.
    transcribe_price_per_minute: float | None = None

    # Reading links (web_fetch): text kept per page; bare links in a message are read
    # before the LLM answers (at most this many).
    web_fetch_max_chars: int = 12000
    link_autofetch: bool = True
    link_autofetch_max: int = 2

    # Search
    search_backend: SearchBackendName = SearchBackendName.AUTO
    search_max_results: int = 5
    wikipedia_lang: str = "pt"

    # Proactive behaviour
    proactive_daily_budget_usd: float = 0.5
    # Minimum spacing between goal runs / initiatives; 0 disables the loop.
    proactive_interval_minutes: int = 15
    # How often beat checks for due goals (cheap; restarts don't delay runs).
    proactive_poll_minutes: int = 5
    # Goal runs may ask for a new command (self-coding) when they need one.
    proactive_allow_selfcode: bool = True
    # Initiatives: messages the bot sends on its own (research on your interests,
    # follow-ups, reminders). Toggle at runtime with /proativo on|off.
    proactive_messages_enabled: bool = True
    proactive_max_messages_per_day: int = 8
    proactive_min_gap_minutes: int = 60
    # The cheap model is asked "anything worth sending?" at most this often (each
    # ask costs budget; asking every 5 minutes burned the whole proactive budget).
    proactive_decide_every_minutes: int = 40
    # After this long without an initiative, "nothing" is not an option.
    proactive_force_after_hours: float = 3.0
    # Quiet hours (local time, TIMEZONE): no goal runs or initiatives.
    proactive_quiet_start_hour: int = 23
    proactive_quiet_end_hour: int = 8
    proactive_max_steps: int = 3  # LLM rounds per goal run
    proactive_max_tokens_per_run: int = 30000
    # Tell the owner on Telegram when background work (incl. goal runs) finishes.
    proactive_notify: bool = True
    # Recurring goals with unchanged results run less often, up to this interval.
    goal_max_interval_hours: float = 48

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

    # Short-term memory (Redis session, see docs/memoria-curto-prazo.md)
    stm_ttl_hours: float = 12
    stm_max_turns: int = 30
    stm_max_refs: int = 15
    stm_promote_idle_hours: float = 3

    # Background review of the conversation (memory, skills, profile) and the
    # nightly journal digest.
    review_idle_minutes: int = 10
    review_min_turns: int = 4
    review_every_turns: int = 8
    # off | on (one short line when something was kept) | verbose (with the reason)
    review_notify: Literal["off", "on", "verbose"] = "on"

    # Routines: the same failure alerts once, then reminds every N hours.
    routine_failure_remind_hours: float = 6

    # Telegram: reactions on your message as feedback (👀 received, 👍 done, 👎 error).
    telegram_reactions: bool = True
    # After a restart, turns cut short this recently get a "do it again?" question.
    recover_max_hours: float = 6
    # Plain messages sent in a row wait this long of quiet and go to the router as
    # one turn (long pastes, forwards and albums wait a bit more); 0 turns it off.
    inbound_debounce_ms: int = 300
    # Documents sent on Telegram (PDF, DOCX, TXT, MD, CSV, JSON).
    document_max_mb: float = 10
    document_max_chars: int = 60000
    # How much of a document's text goes with the message (document_read reads the rest).
    document_context_chars: int = 8000
    # Voice replies: off | voice_in (when you sent a voice note) | always.
    voice_replies: Literal["off", "voice_in", "always"] = "off"
    tts_model: str = "gpt-4o-mini-tts"
    tts_voice: str = "alloy"
    tts_api_key: SecretStr | None = None
    tts_price_per_1k_chars: float = 0.015
    # Plugins and skills unused for this many days leave /help and the prompt (still usable).
    plugin_stale_days: int = 30

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
    # User profile, always in the prompt; empty means <repo>/data/profile.md.
    profile_path: str = ""
    # Size caps for the always-loaded files: writes (self-edit) that would go
    # over are refused; /perfil consolidar shrinks the profile to fit.
    profile_max_chars: int = 4000
    soul_max_chars: int = 3000
    # Self-coding (/feature): plugins written, tested and merged by the bot.
    selfcode_daily_budget_usd: float = 2.0
    selfcode_max_attempts: int = 3
    selfcode_test_timeout_seconds: int = 300
    selfcode_time_limit_seconds: int = 1800

    # Projects (/projeto): NEW projects, outside this repository, built by Claude Code
    # in the "projects" worker. The folder is mounted there at projects_path; the host
    # path is only shown in messages.
    projects_enabled: bool = True
    projects_path: str = "/data/projects"
    projects_host_path: str = "../uva-projects"
    # Claude Code credential (one of them): `claude setup-token` or an API key.
    claude_code_oauth_token: SecretStr | None = None
    projects_anthropic_api_key: SecretStr | None = None
    project_claude_bin: str = "claude"
    project_claude_model: str = ""
    project_run_as_user: str = "builder"
    project_timeout_minutes: int = 30
    project_max_turns: int = 80
    project_max_budget_usd: float = 3.0
    project_daily_budget_usd: float = 10.0

    # /sh and /py: the bot's own folder /uva, in the isolated "shell" container.
    shell_enabled: bool = True
    shell_url: str = "http://shell:8700"
    shell_timeout_seconds: int = 60

    # MCP servers (data/mcp.json), run by the isolated "mcp" gateway container.
    mcp_enabled: bool = True
    mcp_url: str = "http://mcp:8790"
    # Shared with the gateway (compose passes this one value to it); required.
    mcp_gateway_token: SecretStr | None = None
    # Above this many MCP tools the prompt lists servers (and mcp_tools) instead.
    mcp_prompt_tool_limit: int = 30

    # Local dashboard (http://localhost:8080); optional password (HTTP Basic, any user).
    dashboard_password: SecretStr | None = None

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
    def vision_model(self) -> str | None:
        return self.llm_model_vision or self.router_model

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

    @field_validator("bot_language")
    @classmethod
    def _check_language(cls, value: str) -> str:
        from uva.i18n import normalize

        return normalize(value)

    @property
    def phrases_file(self) -> Path:
        """Ready-made phrases: data/fallback_phrases.json (pt) or fallback_phrases.<lang>.json."""
        suffix = "" if self.bot_language == "pt" else f".{self.bot_language}"
        return self.repo_dir / "data" / f"fallback_phrases{suffix}.json"

    @property
    def soul_file(self) -> Path:
        return Path(self.soul_path) if self.soul_path else self.repo_dir / "data" / "soul.md"

    @property
    def skills_dir(self) -> Path:
        """Skills (procedural memory): <repo>/data/skills/<name>/SKILL.md."""
        return self.repo_dir / "data" / "skills"

    @property
    def profile_file(self) -> Path:
        """The user's profile (data/profile.md), always loaded like soul.md; gitignored."""
        return Path(self.profile_path) if self.profile_path else self.repo_dir / "data" / "profile.md"

    @property
    def llm_default_price(self) -> tuple[float, float]:
        return (self.llm_default_price_input, self.llm_default_price_output)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    # pydantic fills required fields from env; mypy can't see that.
    return Settings()  # type: ignore[call-arg]
