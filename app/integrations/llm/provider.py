"""Assembling the selected LLM adapter. Pure: no network call and no billable request at
startup (the first real request proves connectivity; a refused key then surfaces as
AUTH_INVALID on that request). The configured provider and model are authoritative: there
is no fallback to another provider or model."""

from typing import Any

from pydantic import SecretStr

from app.integrations.config import LLMProviderConfig
from app.integrations.llm.base import HttpLLMTransport
from app.integrations.providers import LLMProviderId
from app.integrations.secrets import LLMSecrets


class LLMConfigurationError(Exception):
    """Selected but unusable configuration (normally caught earlier by ``evaluate``)."""


def build_llm(config: LLMProviderConfig, secrets: LLMSecrets, *, session: Any = None) -> HttpLLMTransport:
    """``session``: the HTTP session to use (tests inject a fake one); None = a real one,
    created lazily on the first request."""
    if config.model is None or secrets.api_key is None:
        raise LLMConfigurationError("LLM_MODEL and LLM_API_KEY are required")
    key: SecretStr = secrets.api_key
    common = {"api_key": key, "model": config.model, "timeout_seconds": config.timeout_seconds,
              "max_output_tokens": config.max_output_tokens, "session": session}
    if config.provider is LLMProviderId.OPENAI:
        from app.integrations.llm.openai import OpenAITransport

        return OpenAITransport(**common)
    if config.provider is LLMProviderId.ANTHROPIC:
        from app.integrations.llm.anthropic import AnthropicTransport

        return AnthropicTransport(**common)
    if config.provider is LLMProviderId.GEMINI:
        from app.integrations.llm.gemini import GeminiTransport

        return GeminiTransport(**common)
    raise LLMConfigurationError(f"no adapter for {config.provider.value}")
