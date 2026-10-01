"""Installed-app OAuth for one Gmail user mailbox (google-auth / google-auth-oauthlib).

- The OAuth client comes from exactly one source (Stage 15): the local client file
  (``GMAIL_CREDENTIALS_FILE``) or the ``GMAIL_CLIENT_ID``/``GMAIL_CLIENT_SECRET`` pair.
- The token lives in ``GMAIL_TOKEN_FILE``. Without a token file, a ``GMAIL_REFRESH_TOKEN``
  secret can bootstrap one (headless deployments).
- ``credentials()`` returns valid credentials: an expired token is refreshed through the
  official library (bounded timeout) and the refreshed token is written atomically before
  it is used; a failed refresh keeps the old file and raises AUTH_REFRESH_FAILED.
- The interactive flow (``authorize``) runs only from the explicit ``gmail-auth`` command:
  never at startup or from any other command.
Errors carry codes only: no token, secret or library message.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from google.auth.exceptions import GoogleAuthError, RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from app.integrations.gmail.errors import Delivery, GmailCode, GmailError
from app.integrations.gmail.tokens import SCOPES, has_scopes, read_token, write_token

TOKEN_URI = "https://oauth2.googleapis.com/token"
AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
INTERACTIVE_TIMEOUT_SECONDS = 300


@dataclass(frozen=True)
class GmailAuthSettings:
    token_file: Path
    credentials_file: Path | None = None
    client_id: str | None = None
    client_secret: str | None = None
    refresh_token: str | None = None
    timeout_seconds: int = 30

    def __repr__(self) -> str:  # never show secrets, even in a debugger or traceback
        return f"GmailAuthSettings(token_file=<set>, credentials_file={'<set>' if self.credentials_file else None})"


def client_config(settings: GmailAuthSettings) -> dict[str, dict[str, object]]:
    """The OAuth client in Google's "installed" shape; CLIENT_CONFIG_INVALID otherwise."""
    if settings.credentials_file is not None:
        try:
            data = json.loads(settings.credentials_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            raise GmailError(GmailCode.CLIENT_CONFIG_INVALID, delivery=Delivery.NOT_SENT) from None
        section = data.get("installed") if isinstance(data, dict) else None
        if not isinstance(section, dict) or not section.get("client_id") or not section.get("client_secret"):
            raise GmailError(GmailCode.CLIENT_CONFIG_INVALID, delivery=Delivery.NOT_SENT)
        return {"installed": {"auth_uri": AUTH_URI, "token_uri": TOKEN_URI, **section}}
    if not settings.client_id or not settings.client_secret:
        raise GmailError(GmailCode.CLIENT_CONFIG_INVALID, delivery=Delivery.NOT_SENT)
    return {"installed": {"client_id": settings.client_id, "client_secret": settings.client_secret,
                          "auth_uri": AUTH_URI, "token_uri": TOKEN_URI, "redirect_uris": ["http://localhost"]}}


class _BoundedRequest(Request):
    """google-auth's transport for token refresh, with a fixed timeout."""

    def __init__(self, timeout: int) -> None:
        super().__init__()
        self._timeout = timeout

    def __call__(self, *args: object, **kwargs: object):  # noqa: ANN204
        kwargs["timeout"] = self._timeout
        return super().__call__(*args, **kwargs)  # type: ignore[arg-type]


class GmailAuth:
    def __init__(self, settings: GmailAuthSettings, *, refresh_request: Callable[[], Request] | None = None) -> None:
        self._settings = settings
        self._request = refresh_request or (lambda: _BoundedRequest(settings.timeout_seconds))
        self._credentials: Credentials | None = None

    def __repr__(self) -> str:
        return "GmailAuth(<redacted>)"

    def credentials(self) -> Credentials:
        """Valid credentials, refreshing (and persisting) when needed. No interactive flow."""
        credentials = self._credentials or self._load()
        if not credentials.valid:
            self._refresh(credentials)
        self._credentials = credentials
        return credentials

    def _load(self) -> Credentials:
        settings = self._settings
        try:
            data = read_token(settings.token_file)
        except ValueError:
            raise GmailError(GmailCode.AUTH_INVALID, delivery=Delivery.NOT_SENT) from None
        if data is None:
            if not settings.refresh_token:
                raise GmailError(GmailCode.AUTH_REQUIRED, delivery=Delivery.NOT_SENT)
            client = client_config(settings)["installed"]
            return Credentials(None, refresh_token=settings.refresh_token, token_uri=TOKEN_URI, client_id=str(client["client_id"]),
                               client_secret=str(client["client_secret"]), scopes=list(SCOPES))
        if not data.get("refresh_token") or not has_scopes(data):
            raise GmailError(GmailCode.AUTH_INVALID, delivery=Delivery.NOT_SENT)
        try:
            return Credentials.from_authorized_user_info(data, scopes=list(SCOPES))
        except (ValueError, GoogleAuthError):
            raise GmailError(GmailCode.AUTH_INVALID, delivery=Delivery.NOT_SENT) from None

    def _refresh(self, credentials: Credentials) -> None:
        try:
            credentials.refresh(self._request())
        except (RefreshError, TransportError, GoogleAuthError, OSError):
            raise GmailError(GmailCode.AUTH_REFRESH_FAILED, delivery=Delivery.NOT_SENT) from None
        if not credentials.valid:
            raise GmailError(GmailCode.AUTH_REFRESH_FAILED, delivery=Delivery.NOT_SENT)
        save(self._settings.token_file, credentials)


def save(token_file: Path, credentials: Credentials) -> None:
    write_token(token_file, credentials.to_json())


Flow = Callable[[dict[str, dict[str, object]], tuple[str, ...]], Credentials]


def run_installed_app_flow(config: dict[str, dict[str, object]], scopes: tuple[str, ...]) -> Credentials:
    """Google's installed-app flow: a local redirect server on a free port and the user's
    browser. Nothing here collects a password."""
    from google_auth_oauthlib.flow import InstalledAppFlow  # imported only for gmail-auth

    flow = InstalledAppFlow.from_client_config(config, list(scopes))
    credentials = flow.run_local_server(port=0, open_browser=True, timeout_seconds=INTERACTIVE_TIMEOUT_SECONDS)
    return credentials  # type: ignore[return-value]


def authorize(settings: GmailAuthSettings, *, flow: Flow = run_installed_app_flow) -> Credentials:
    """The explicit interactive authorization. Returns credentials; the caller verifies the
    account before saving them. Fails clearly when the flow cannot complete."""
    config = client_config(settings)
    try:
        credentials = flow(config, SCOPES)
    except GmailError:
        raise
    except Exception:  # noqa: BLE001 - browser unavailable, timeout, user denied, ...: never a fake success
        raise GmailError(GmailCode.AUTHORIZATION_NOT_COMPLETED, delivery=Delivery.NOT_SENT) from None
    if credentials is None or not credentials.refresh_token or not set(SCOPES) <= set(credentials.scopes or ()):
        raise GmailError(GmailCode.AUTHORIZATION_NOT_COMPLETED, delivery=Delivery.NOT_SENT)
    return credentials
