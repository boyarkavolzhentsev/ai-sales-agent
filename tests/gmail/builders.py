"""Gmail-selected runtimes over the fake Gmail API. The token file is a fake authorized-user
JSON in pytest's temporary directory; nothing contacts Google (tests/conftest.py blocks
the network)."""

import json
from datetime import datetime
from pathlib import Path

from app.integrations import ProviderConnectors
from app.integrations.gmail.tokens import SCOPES
from app.persistence import FrozenClock
from app.runtime import Adapters, SalesAgentRuntime, load_config
from tests.gmail.fakes import ACCOUNT, FakeGmailApi
from tests.inbound.builders import NOW
from tests.operator.builders import FakeAuthenticator
from tests.runtime.builders import env

CLIENT_ID = "test-secret-do-not-use-gmail-client-id.apps.example"
CLIENT_SECRET = "test-secret-do-not-use-gmail-client-secret"
ACCESS_TOKEN = "test-secret-do-not-use-gmail-access-token"
REFRESH_TOKEN = "test-secret-do-not-use-gmail-refresh-token"
GMAIL_SECRETS = (CLIENT_SECRET, ACCESS_TOKEN, REFRESH_TOKEN)


def token_json(*, scopes: tuple[str, ...] = SCOPES, refresh: bool = True, expiry: str = "2999-01-01T00:00:00Z") -> str:
    data: dict[str, object] = {"token": ACCESS_TOKEN, "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
                               "token_uri": "https://oauth2.googleapis.com/token", "scopes": list(scopes), "expiry": expiry}
    if refresh:
        data["refresh_token"] = REFRESH_TOKEN
    return json.dumps(data)


def gmail_values(tmp_path: Path, *, token: bool = True, **overrides: str | None) -> dict[str, str | None]:
    directory = tmp_path / "credentials"
    directory.mkdir(exist_ok=True)
    token_file = directory / "gmail-token.json"
    if token:
        token_file.write_text(token_json(), encoding="utf-8")
    client = directory / "gmail-oauth-client.json"
    client.write_text(json.dumps({"installed": {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}}), encoding="utf-8")
    return {"EMAIL_PROVIDER": "gmail", "GMAIL_ADDRESS": ACCOUNT, "GMAIL_TOKEN_FILE": str(token_file),
            "GMAIL_CREDENTIALS_FILE": str(client)} | overrides


def gmail_env(tmp_path: Path, db_name: str = "agent.sqlite3", *, token: bool = True, **overrides: str | None) -> dict[str, str]:
    return env(tmp_path / db_name, **(gmail_values(tmp_path, token=token) | overrides))


def connectors(api: FakeGmailApi) -> ProviderConnectors:
    return ProviderConnectors(gmail_api=lambda auth, timeout: api)


def gmail_runtime(tmp_path: Path, api: FakeGmailApi, *, adapters: Adapters | None = None, at: datetime = NOW,
                  clock: FrozenClock | None = None, db_name: str = "agent.sqlite3", token: bool = True,
                  **overrides: str | None) -> SalesAgentRuntime:
    config = load_config(gmail_env(tmp_path, db_name, token=token, **overrides), now=at)
    return SalesAgentRuntime(config, adapters=adapters or Adapters(authenticator=FakeAuthenticator()),
                             clock=clock or FrozenClock(at), connectors=connectors(api))
