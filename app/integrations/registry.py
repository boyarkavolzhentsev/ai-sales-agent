"""The provider factory boundary.

``build_provider_adapters`` is the one place a selected provider becomes an adapter
implementing an existing contract (``EmailTransport``/``DispatchReconciler``,
``LLMTransport``, ``OperatorAuthenticator``). No real provider adapter exists yet, so it
returns absent adapters and names every selected provider that has no implementation.
It never constructs a client, never opens a connection and never substitutes a fake: a
configured provider is not an implemented provider.
"""

from dataclasses import dataclass

from app.dispatch import DispatchReconciler, EmailTransport
from app.integrations.config import IntegrationConfig
from app.integrations.providers import KnowledgeProviderId, ProviderCategory
from app.integrations.secrets import ProviderSecrets
from app.llm import LLMTransport
from app.operator import OperatorAuthenticator

# (category, provider id) pairs that have an implementation. LOCAL knowledge is the
# existing Stage 4 index; every external provider arrives in a later stage.
IMPLEMENTED: frozenset[tuple[ProviderCategory, str]] = frozenset({
    (ProviderCategory.KNOWLEDGE, KnowledgeProviderId.LOCAL.value),
})


def is_implemented(category: ProviderCategory, provider: str) -> bool:
    return (category, provider) in IMPLEMENTED


@dataclass(frozen=True)
class ProviderAdapters:
    email_transport: EmailTransport | None = None
    reconciler: DispatchReconciler | None = None
    llm_transport: LLMTransport | None = None
    authenticator: OperatorAuthenticator | None = None
    # Selected providers whose adapter does not exist yet: (category, provider id).
    not_implemented: tuple[tuple[ProviderCategory, str], ...] = ()


def selected(config: IntegrationConfig) -> tuple[tuple[ProviderCategory, str], ...]:
    """Every category's selected provider ID, in category order."""
    return (
        (ProviderCategory.EMAIL, config.email.provider.value),
        (ProviderCategory.LLM, config.llm.provider.value),
        (ProviderCategory.OPERATOR_CHANNEL, config.operator.provider.value),
        (ProviderCategory.KNOWLEDGE, config.knowledge.provider.value),
        (ProviderCategory.EMBEDDINGS, config.embeddings.provider.value),
    )


def build_provider_adapters(config: IntegrationConfig, secrets: ProviderSecrets) -> ProviderAdapters:
    """Adapters for the selected providers. Pure: no I/O. ``secrets`` will be handed to
    the provider modules once they exist; nothing reads them here."""
    del secrets  # no adapter consumes them yet
    missing = tuple((category, provider) for category, provider in selected(config)
                    if provider != "NONE" and not is_implemented(category, provider))
    return ProviderAdapters(not_implemented=missing)
