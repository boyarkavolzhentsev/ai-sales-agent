"""Assembling the Gmail adapters for one configured mailbox, and the explicit
``gmail-auth`` authorization.

``build_gmail`` loads (and if needed refreshes) the authorization, then confirms with one
read (``users.getProfile``) that the authorized account IS the configured address; any
mismatch fails closed (MAILBOX_MISMATCH): nothing is ever sent from the wrong account. It
never starts the interactive flow. Exactly one Gmail account per process.
"""

from collections.abc import Callable
from dataclasses import dataclass

from app.integrations.config import EmailProviderConfig
from app.integrations.gmail.auth import GmailAuth, GmailAuthSettings, authorize, run_installed_app_flow, save
from app.integrations.gmail.client import GmailApi, GmailClient
from app.integrations.gmail.errors import Delivery, GmailCode, GmailError
from app.integrations.gmail.inbound import GmailMailboxReader
from app.integrations.gmail.reconciliation import GmailReconciler
from app.integrations.gmail.transport import GmailTransport
from app.integrations.secrets import GmailSecrets

ApiFactory = Callable[[GmailAuth, int], GmailApi]


def real_api(auth: GmailAuth, timeout_seconds: int) -> GmailApi:
    return GmailClient(auth, timeout_seconds=timeout_seconds)


@dataclass(frozen=True)
class GmailAdapters:
    address: str
    transport: GmailTransport
    reconciler: GmailReconciler
    reader: GmailMailboxReader


def settings_of(config: EmailProviderConfig, secrets: GmailSecrets) -> GmailAuthSettings:
    if config.token_file is None:
        raise GmailError(GmailCode.AUTH_REQUIRED, delivery=Delivery.NOT_SENT)
    value = lambda s: s.get_secret_value() if s is not None else None  # noqa: E731
    return GmailAuthSettings(token_file=config.token_file, credentials_file=config.credentials_file,
                             client_id=value(secrets.client_id), client_secret=value(secrets.client_secret),
                             refresh_token=value(secrets.refresh_token), timeout_seconds=config.timeout_seconds)


def build_gmail(config: EmailProviderConfig, secrets: GmailSecrets, *, api_factory: ApiFactory = real_api) -> GmailAdapters:
    if config.address is None:
        raise GmailError(GmailCode.MAILBOX_MISMATCH, delivery=Delivery.NOT_SENT)
    auth = GmailAuth(settings_of(config, secrets))
    api = api_factory(auth, config.timeout_seconds)
    if api.profile().email_address.lower() != config.address:
        raise GmailError(GmailCode.MAILBOX_MISMATCH, delivery=Delivery.NOT_SENT)
    return GmailAdapters(address=config.address, transport=GmailTransport(api, address=config.address),
                         reconciler=GmailReconciler(api), reader=GmailMailboxReader(api, address=config.address))


def authorize_mailbox(config: EmailProviderConfig, secrets: GmailSecrets, *, flow=run_installed_app_flow,  # noqa: ANN001
                      api_factory: ApiFactory = real_api) -> str:
    """The explicit gmail-auth step: run the installed-app flow, confirm the authorized
    account is the configured address, and only then store the token. Returns the address."""
    settings = settings_of(config, secrets)
    credentials = authorize(settings, flow=flow)

    class _Fresh(GmailAuth):  # the new credentials, before they are saved anywhere
        def credentials(self):  # noqa: ANN202
            return credentials

    api = api_factory(_Fresh(settings), config.timeout_seconds)
    if config.address is None or api.profile().email_address.lower() != config.address:
        raise GmailError(GmailCode.MAILBOX_MISMATCH, delivery=Delivery.NOT_SENT)  # the token is not stored
    save(settings.token_file, credentials)
    return config.address
