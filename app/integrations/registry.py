"""The provider factory boundary.

``build_provider_adapters`` is the one place a selected provider becomes an adapter
implementing an existing contract (``EmailTransport``/``DispatchReconciler``, the
provider-neutral ``MailboxReader``; later ``LLMTransport`` and ``OperatorAuthenticator``).
It never substitutes a fake: a configured provider is not an implemented one, and a
provider that cannot be used now (e.g. not authorized) raises ``ProviderUnavailableError``.

Implemented: LOCAL knowledge (the Stage 4 index), GMAIL email (Stage 16) and TELEGRAM
operator channel (Stage 17). Provider code is imported only when that provider is
selected, so an offline deployment never loads it. Building Gmail adapters reads the local
token, refreshes it if needed and makes one read (the account profile) to confirm the
mailbox; it never starts the interactive OAuth flow. Building Telegram adapters makes one
``getMe`` call to prove the bot token works.
"""

from dataclasses import dataclass
from typing import Any

from app.dispatch import DispatchReconciler, EmailTransport
from app.integrations.config import IntegrationConfig
from app.integrations.mailbox import MailboxReader
from app.integrations.providers import EmailProviderId, KnowledgeProviderId, OperatorProviderId, ProviderCategory
from app.integrations.secrets import ProviderSecrets
from app.llm import LLMTransport
from app.operator import OperatorAuthenticator

# (category, provider id) pairs that have an implementation.
IMPLEMENTED: frozenset[tuple[ProviderCategory, str]] = frozenset({
    (ProviderCategory.KNOWLEDGE, KnowledgeProviderId.LOCAL.value),
    (ProviderCategory.EMAIL, EmailProviderId.GMAIL.value),
    (ProviderCategory.OPERATOR_CHANNEL, OperatorProviderId.TELEGRAM.value),
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
    a ``GmailApi``; ``telegram_api(token, timeout_seconds)`` a ``TelegramApi``. None means
    the real client."""

    gmail_api: Any = None
    telegram_api: Any = None


@dataclass(frozen=True)
class ProviderAdapters:
    email_transport: EmailTransport | None = None
    reconciler: DispatchReconciler | None = None
    mailbox: MailboxReader | None = None
    operator_channel: Any = None  # TelegramAdapters (api, bot identity, authenticator)
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
    """Adapters for the selected providers. Pure (no I/O) unless Gmail or Telegram is selected."""
    connectors = connectors or ProviderConnectors()
    missing = tuple((category, provider) for category, provider in selected(config)
                    if provider != "NONE" and not is_implemented(category, provider))
    email: dict[str, Any] = {}
    if config.email.provider is EmailProviderId.GMAIL:
        from app.integrations.gmail.errors import GmailError
        from app.integrations.gmail.provider import build_gmail

        try:
            gmail = build_gmail(config.email, secrets.gmail,
                                **({"api_factory": connectors.gmail_api} if connectors.gmail_api else {}))
        except GmailError as exc:
            raise ProviderUnavailableError(ProviderCategory.EMAIL, exc.code.value) from None
        email = {"email_transport": gmail.transport, "reconciler": gmail.reconciler, "mailbox": gmail.reader}
    channel = None
    if config.operator.provider is OperatorProviderId.TELEGRAM:
        from app.integrations.telegram.errors import TelegramError
        from app.integrations.telegram.provider import build_telegram

        try:
            channel = build_telegram(config.operator, secrets.telegram,
                                     **({"api_factory": connectors.telegram_api} if connectors.telegram_api else {}))
        except TelegramError as exc:
            raise ProviderUnavailableError(ProviderCategory.OPERATOR_CHANNEL, exc.code.value) from None
    return ProviderAdapters(**email, operator_channel=channel, not_implemented=missing)
