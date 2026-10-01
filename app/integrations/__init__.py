"""Integration foundation (Stage 15): provider selection, non-secret provider settings,
provider secrets, configuration health and the provider factory boundary.

Configuration only. Nothing here opens a connection, imports a provider SDK, reads a
credential file or constructs a client; no real provider adapter exists yet, so every
selected external provider reports NOT_IMPLEMENTED and its capability stays unavailable.
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
from app.integrations.registry import ProviderAdapters, build_provider_adapters, is_implemented
from app.integrations.secrets import GmailSecrets, LLMSecrets, ProviderSecrets, TelegramSecrets
from app.integrations.status import IntegrationStatus, ProviderState, ProviderStatus, evaluate

__all__ = [
    "INTEGRATION_VARIABLES",
    "SECRET_NAMES",
    "EmailProviderConfig",
    "EmailProviderId",
    "EmbeddingsProviderConfig",
    "EmbeddingsProviderId",
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
    "ProviderCategory",
    "ProviderSecrets",
    "ProviderState",
    "ProviderStatus",
    "TelegramSecrets",
    "build_provider_adapters",
    "evaluate",
    "is_implemented",
    "parse_integrations",
]
