"""The provider factory boundary.

``build_provider_adapters`` is the one place a selected provider becomes an adapter
implementing an existing contract (``EmailTransport``/``DispatchReconciler``, the
provider-neutral ``MailboxReader``; later ``LLMTransport`` and ``OperatorAuthenticator``).
It never substitutes a fake: a configured provider is not an implemented one, and a
provider that cannot be used now (e.g. not authorized) raises ``ProviderUnavailableError``.

Implemented: LOCAL knowledge (the Stage 4 index) and GMAIL email (Stage 16). Gmail code is
imported only when Gmail is selected, so an offline deployment never loads a Google
library. Building Gmail adapters reads the local token, refreshes it if needed and makes
one read (the account profile) to confirm the mailbox; it never starts the interactive
OAuth flow.
"""

from dataclasses import dataclass
from typing import Any

from app.dispatch import DispatchReconciler, EmailTransport
from app.integrations.config import IntegrationConfig
from app.integrations.mailbox import MailboxReader
from app.integrations.providers import EmailProviderId, KnowledgeProviderId, ProviderCategory
from app.integrations.secrets import ProviderSecrets
from app.llm import LLMTransport
from app.operator import OperatorAuthenticator

# (category, provider id) pairs that have an implementation.
IMPLEMENTED: frozenset[tuple[ProviderCategory, str]] = frozenset({
    (ProviderCategory.KNOWLEDGE, KnowledgeProviderId.LOCAL.value),
    (ProviderCategory.EMAIL, EmailProviderId.GMAIL.value),
})


def is_implemented(category: ProviderCategory, provider: str) -> bool:
    return (category, provider) in IMPLEMENTED


class ProviderUnavailableError(Exception):
    """A selected, implemented provider cannot be used now. ``code`` is stable and safe."""

    def __init__(self, category: ProviderCategory, code: str) -> None:
        self.category = category
        self.code = code
        super().__init__(f"{category.value}:{code}")


@dataclass(frozen=True)
class ProviderConnectors:
    """Seam beneath the adapters (tests): ``gmail_api(GmailAuth, timeout_seconds)`` returns
    a ``GmailApi`` instead of the real REST client. None means the real client."""

    gmail_api: Any = None


@dataclass(frozen=True)
class ProviderAdapters:
    email_transport: EmailTransport | None = None
    reconciler: DispatchReconciler | None = None
    mailbox: MailboxReader | None = None
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


def build_provider_adapters(config: IntegrationConfig, secrets: ProviderSecrets,
                            connectors: ProviderConnectors | None = None) -> ProviderAdapters:
    """Adapters for the selected providers. Pure (no I/O) unless Gmail is selected."""
    missing = tuple((category, provider) for category, provider in selected(config)
                    if provider != "NONE" and not is_implemented(category, provider))
    if config.email.provider is not EmailProviderId.GMAIL:
        return ProviderAdapters(not_implemented=missing)
    from app.integrations.gmail.errors import GmailError
    from app.integrations.gmail.provider import build_gmail

    factory = (connectors or ProviderConnectors()).gmail_api
    try:
        gmail = build_gmail(config.email, secrets.gmail, **({"api_factory": factory} if factory else {}))
    except GmailError as exc:
        raise ProviderUnavailableError(ProviderCategory.EMAIL, exc.code.value) from None
    return ProviderAdapters(email_transport=gmail.transport, reconciler=gmail.reconciler, mailbox=gmail.reader,
                            not_implemented=missing)
