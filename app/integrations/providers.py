"""Provider categories and the concrete provider IDs a deployment may select.

Only providers the existing contracts can host are listed: the email transport and
reconciler (Stage 8), the LLM transport (Stage 5), the operator authenticator (Stage 7),
the local knowledge index (Stage 4), the embeddings transport (Stage 19). Selecting a provider is configuration; whether an
implementation exists is a separate fact (see ``app.integrations.registry``).
"""

from enum import StrEnum


class ProviderCategory(StrEnum):
    EMAIL = "EMAIL"
    LLM = "LLM"
    OPERATOR_CHANNEL = "OPERATOR_CHANNEL"
    KNOWLEDGE = "KNOWLEDGE"
    EMBEDDINGS = "EMBEDDINGS"


class EmailProviderId(StrEnum):
    NONE = "NONE"
    GMAIL = "GMAIL"


class LLMProviderId(StrEnum):
    NONE = "NONE"
    OPENAI = "OPENAI"
    ANTHROPIC = "ANTHROPIC"
    GEMINI = "GEMINI"


class OperatorProviderId(StrEnum):
    NONE = "NONE"
    TELEGRAM = "TELEGRAM"


class KnowledgeProviderId(StrEnum):
    """LOCAL: approved knowledge ingested into the local SQLite index (Stage 4), the only
    knowledge store that exists. An external store is a future addition."""

    LOCAL = "LOCAL"


class EmbeddingsProviderId(StrEnum):
    """Embeddings for semantic retrieval over the LOCAL knowledge index (Stage 19). NONE keeps
    the lexical Stage 4 retrieval. Independent of the LLM provider (e.g. an Anthropic LLM with
    OpenAI embeddings). Anthropic has no first-party embeddings API, so it is not listed."""

    NONE = "NONE"
    OPENAI = "OPENAI"
    GEMINI = "GEMINI"
