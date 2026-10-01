"""Provider categories and the concrete provider IDs a deployment may select.

Only providers the existing contracts can host are listed: the email transport and
reconciler (Stage 8), the LLM transport (Stage 5), the operator authenticator (Stage 7),
the local knowledge index (Stage 4). Selecting a provider is configuration; whether an
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
    """No embeddings provider exists yet (Stage 4 retrieval is lexical)."""

    NONE = "NONE"
