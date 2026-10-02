"""Integration foundation (Stage 15): provider selection, non-secret provider settings,
provider secrets, configuration health and the provider factory boundary.

The modules directly in this package are configuration only: nothing opens a connection,
imports a provider SDK or reads a credential file. Real provider code lives in provider
subpackages (``gmail``, Stage 16), loaded only when that provider is selected. Selected
providers without an implementation (LLM, Telegram) report NOT_IMPLEMENTED.
Dependency direction: runtime -> integrations -> provider-neutral contracts.
"""

from app.integrations.config import (
    EmailProviderConfig,
    EmbeddingsProviderConfig,
    IntegrationConfig,
    KnowledgeProviderConfig,
    LLMProviderConfig,
    OperatorChannelConfig,
)
from app.integrations.env import INTEGRATION_VARIABLES, SECRET_NAMES, ParsedIntegrations, parse_integrations
from app.integrations.errors import IntegrationCode, IntegrationProblem
from app.integrations.providers import (
    EmailProviderId,
    EmbeddingsProviderId,
    KnowledgeProviderId,
    LLMProviderId,
    OperatorProviderId,
    ProviderCategory,
)
from app.integrations.registry import (
    ProviderAdapters,
    ProviderConnectors,
    ProviderUnavailableError,
    build_embeddings_adapter,
    build_llm_adapter,
    build_provider_adapters,
    is_implemented,
)
from app.integrations.secrets import EmbeddingsSecrets, GmailSecrets, LLMSecrets, ProviderSecrets, TelegramSecrets
from app.integrations.status import IntegrationStatus, ProviderState, ProviderStatus, evaluate

__all__ = [
    "INTEGRATION_VARIABLES",
    "SECRET_NAMES",
    "EmailProviderConfig",
    "EmailProviderId",
    "EmbeddingsProviderConfig",
    "EmbeddingsProviderId",
    "EmbeddingsSecrets",
    "GmailSecrets",
    "IntegrationCode",
    "IntegrationConfig",
    "IntegrationProblem",
    "IntegrationStatus",
    "KnowledgeProviderConfig",
    "KnowledgeProviderId",
    "LLMProviderConfig",
    "LLMProviderId",
    "LLMSecrets",
    "OperatorChannelConfig",
    "OperatorProviderId",
    "ParsedIntegrations",
    "ProviderAdapters",
    "ProviderConnectors",
    "ProviderUnavailableError",
    "ProviderCategory",
    "ProviderSecrets",
    "ProviderState",
    "ProviderStatus",
    "TelegramSecrets",
    "build_embeddings_adapter",
    "build_llm_adapter",
    "build_provider_adapters",
    "evaluate",
    "is_implemented",
    "parse_integrations",
]
