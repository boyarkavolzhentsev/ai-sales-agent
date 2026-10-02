"""Assembling the selected embeddings adapter. Pure: no network call and no billable request
at startup (the first ``knowledge-index`` run or semantic retrieval proves connectivity).
The configured provider, model and dimensionality are authoritative: there is no fallback
to another provider or model, and the LLM's key is never used."""

from typing import Any

from app.integrations.config import EmbeddingsProviderConfig
from app.integrations.embeddings.base import HttpEmbeddingTransport
from app.integrations.providers import EmbeddingsProviderId
from app.integrations.secrets import EmbeddingsSecrets


class EmbeddingsConfigurationError(Exception):
    """Selected but unusable configuration (normally caught earlier by ``evaluate``)."""


def build_embeddings(config: EmbeddingsProviderConfig, secrets: EmbeddingsSecrets, *,
                     session: Any = None) -> HttpEmbeddingTransport:
    """``session``: the HTTP session to use (tests inject a fake one); None = a real one,
    created lazily on the first request."""
    if config.model is None or secrets.api_key is None:
        raise EmbeddingsConfigurationError("EMBEDDINGS_MODEL and EMBEDDINGS_API_KEY are required")
    common = {"api_key": secrets.api_key, "model": config.model, "dimensions": config.dimensions,
              "timeout_seconds": config.timeout_seconds, "session": session}
    if config.provider is EmbeddingsProviderId.OPENAI:
        from app.integrations.embeddings.openai import OpenAIEmbeddings

        return OpenAIEmbeddings(**common)
    if config.provider is EmbeddingsProviderId.GEMINI:
        from app.integrations.embeddings.gemini import GeminiEmbeddings

        return GeminiEmbeddings(**common)
    raise EmbeddingsConfigurationError(f"no adapter for {config.provider.value}")
