"""Which OpenAI key to use for OpenAI-only APIs (Decisions, transcription, vision)."""

from __future__ import annotations

from pydantic import SecretStr

from uva.config import LLMProvider, Settings

OPENAI_BASE_URL = "https://api.openai.com/v1"


def resolve_openai_key(settings: Settings, explicit: SecretStr | None) -> str:
    """The feature's own key, else LLM_API_KEY when the main provider is OpenAI itself."""
    key = explicit.get_secret_value().strip() if explicit else ""
    if key:
        return key
    if settings.llm_provider is LLMProvider.OPENAI_COMPATIBLE and settings.llm_api_key:
        if "api.openai.com" in (settings.llm_base_url or OPENAI_BASE_URL):
            return settings.llm_api_key.get_secret_value().strip()
    return ""
