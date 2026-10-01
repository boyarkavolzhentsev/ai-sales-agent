"""Gmail OAuth: client configuration, token loading, refresh through google-auth's real
code with a fake token endpoint, atomic persistence, the explicit interactive flow and the
gmail-auth command. Nothing contacts Google."""

import io
import json
import os
from pathlib import Path

import pytest
from google.oauth2.credentials import Credentials

from app.integrations.gmail import auth as gmail_auth
from app.integrations.gmail import provider as gmail_provider
from app.integrations.gmail.auth import GmailAuth, GmailAuthSettings, authorize, client_config
from app.integrations.gmail.errors import GmailCode, GmailError
from app.integrations.gmail.tokens import SCOPES, Authorization, local_authorization, write_token
from app.runtime import load_config
from app.runtime.cli import main
from tests.gmail.builders import ACCESS_TOKEN, CLIENT_ID, CLIENT_SECRET, GMAIL_SECRETS, REFRESH_TOKEN, gmail_env, token_json
from tests.gmail.fakes import FakeGmailApi
from tests.inbound.builders import NOW

NEW_ACCESS = "test-secret-do-not-use-refreshed-access-token"


class TokenEndpoint:
    """google-auth's transport contract: request(url, method, body, headers) -> response."""

    def __init__(self, status: int = 200, payload: dict[str, object] | None = None) -> None:
        self.status = status
        self.payload = payload if payload is not None else {"access_token": NEW_ACCESS, "expires_in": 3600}
        self.calls = 0

    def __call__(self, url: str, method: str = "GET", body: object = None, headers: object = None, **kwargs: object):  # noqa: ANN204
        self.calls += 1
        endpoint = self

        class Response:
            status = endpoint.status
            data = json.dumps(endpoint.payload).encode()
            headers: dict[str, str] = {}

        return Response()


def settings(tmp_path: Path, **overrides: object) -> GmailAuthSettings:
    client = tmp_path / "client.json"
    client.write_text(json.dumps({"installed": {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}}), encoding="utf-8")
    values: dict[str, object] = {"token_file": tmp_path / "token.json", "credentials_file": client}
    return GmailAuthSettings(**(values | overrides))  # type: ignore[arg-type]


def assert_clean(text: str) -> None:
    for secret in (*GMAIL_SECRETS, NEW_ACCESS):
        assert secret not in text


# ---- Client configuration ---------------------------------------------------------------------


def test_client_configuration_sources(tmp_path: Path) -> None:
    config = client_config(settings(tmp_path))["installed"]
    assert config["client_id"] == CLIENT_ID and config["token_uri"] == "https://oauth2.googleapis.com/token"
    pair = client_config(GmailAuthSettings(token_file=tmp_path / "t.json", client_id="id", client_secret="s"))["installed"]
    assert (pair["client_id"], pair["client_secret"]) == ("id", "s")
    for broken in ("not json", json.dumps({"web": {}}), json.dumps({"installed": {"client_id": CLIENT_ID}})):
        (tmp_path / "client.json").write_text(broken, encoding="utf-8")
        with pytest.raises(GmailError) as error:
            client_config(GmailAuthSettings(token_file=tmp_path / "t.json", credentials_file=tmp_path / "client.json"))
        assert error.value.code is GmailCode.CLIENT_CONFIG_INVALID and CLIENT_ID not in str(error.value)
    with pytest.raises(GmailError) as missing:
        client_config(GmailAuthSettings(token_file=tmp_path / "t.json", credentials_file=tmp_path / "absent.json"))
    assert missing.value.code is GmailCode.CLIENT_CONFIG_INVALID


# ---- Token loading and refresh ---------------------------------------------------------------------


def test_missing_token_requires_authorization(tmp_path: Path) -> None:
    with pytest.raises(GmailError) as error:
        GmailAuth(settings(tmp_path)).credentials()
    assert error.value.code is GmailCode.AUTH_REQUIRED
    assert local_authorization(tmp_path / "token.json", refresh_secret=False) is Authorization.AUTH_REQUIRED


@pytest.mark.parametrize(("content", "state"), [
    ("{not json", Authorization.AUTH_INVALID),
    (token_json(refresh=False), Authorization.AUTH_INVALID),
    (token_json(scopes=("https://www.googleapis.com/auth/gmail.send",)), Authorization.AUTH_INVALID),
    (token_json(), Authorization.AUTHORIZED),
])
def test_local_authorization_reads_only_the_token_file(tmp_path: Path, content: str, state: Authorization) -> None:
    (tmp_path / "token.json").write_text(content, encoding="utf-8")
    assert local_authorization(tmp_path / "token.json", refresh_secret=False) is state
    if state is Authorization.AUTH_INVALID:
        with pytest.raises(GmailError) as error:
            GmailAuth(settings(tmp_path)).credentials()
        assert error.value.code is GmailCode.AUTH_INVALID


def test_a_valid_token_is_used_without_refreshing(tmp_path: Path) -> None:
    (tmp_path / "token.json").write_text(token_json(), encoding="utf-8")
    endpoint = TokenEndpoint()
    credentials = GmailAuth(settings(tmp_path), refresh_request=lambda: endpoint).credentials()
    assert credentials.valid and credentials.token == ACCESS_TOKEN and endpoint.calls == 0


def test_an_expired_token_is_refreshed_and_persisted_atomically(tmp_path: Path) -> None:
    (tmp_path / "token.json").write_text(token_json(expiry="2020-01-01T00:00:00Z"), encoding="utf-8")
    endpoint = TokenEndpoint()
    credentials = GmailAuth(settings(tmp_path), refresh_request=lambda: endpoint).credentials()
    assert credentials.valid and credentials.token == NEW_ACCESS and endpoint.calls == 1
    stored = json.loads((tmp_path / "token.json").read_text(encoding="utf-8"))
    assert stored["token"] == NEW_ACCESS and stored["refresh_token"] == REFRESH_TOKEN
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".gmail-token-")] == []  # no temp files left


def test_a_failed_refresh_keeps_the_previous_token(tmp_path: Path) -> None:
    original = token_json(expiry="2020-01-01T00:00:00Z")
    (tmp_path / "token.json").write_text(original, encoding="utf-8")
    endpoint = TokenEndpoint(status=400, payload={"error": "invalid_grant", "error_description": REFRESH_TOKEN})
    with pytest.raises(GmailError) as error:
        GmailAuth(settings(tmp_path), refresh_request=lambda: endpoint).credentials()
    assert error.value.code is GmailCode.AUTH_REFRESH_FAILED and error.value.__cause__ is None
    assert_clean(str(error.value) + repr(error.value))
    assert (tmp_path / "token.json").read_text(encoding="utf-8") == original


def test_a_refresh_secret_bootstraps_the_token_file(tmp_path: Path) -> None:
    endpoint = TokenEndpoint()
    auth = GmailAuth(settings(tmp_path, refresh_token=REFRESH_TOKEN), refresh_request=lambda: endpoint)
    assert auth.credentials().token == NEW_ACCESS and (tmp_path / "token.json").exists()
    assert local_authorization(tmp_path / "token.json", refresh_secret=False) is Authorization.AUTHORIZED


def test_a_failed_token_write_never_destroys_the_old_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "token.json"
    path.write_text("old", encoding="utf-8")

    def broken(*args: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", broken)
    with pytest.raises(OSError):
        write_token(path, "new")
    assert path.read_text(encoding="utf-8") == "old" and len(list(tmp_path.iterdir())) == 1


def test_auth_objects_never_show_secrets(tmp_path: Path) -> None:
    configured = settings(tmp_path, client_id=CLIENT_ID, client_secret=CLIENT_SECRET, refresh_token=REFRESH_TOKEN)
    assert_clean(repr(configured) + str(configured) + repr(GmailAuth(configured)))


# ---- Interactive authorization (explicit only) -------------------------------------------------------


def fresh_credentials(scopes: tuple[str, ...] = SCOPES, refresh: str | None = REFRESH_TOKEN) -> Credentials:
    return Credentials(ACCESS_TOKEN, refresh_token=refresh, token_uri="https://oauth2.googleapis.com/token",
                       client_id=CLIENT_ID, client_secret=CLIENT_SECRET, scopes=list(scopes))


def test_authorize_needs_a_complete_flow(tmp_path: Path) -> None:
    assert authorize(settings(tmp_path), flow=lambda config, scopes: fresh_credentials()).refresh_token == REFRESH_TOKEN

    def no_browser(config: object, scopes: object) -> Credentials:
        raise RuntimeError("could not locate runnable browser")

    for flow in (no_browser, lambda c, s: fresh_credentials(refresh=None),
                 lambda c, s: fresh_credentials(scopes=("https://www.googleapis.com/auth/gmail.send",))):
        with pytest.raises(GmailError) as error:
            authorize(settings(tmp_path), flow=flow)
        assert error.value.code is GmailCode.AUTHORIZATION_NOT_COMPLETED  # never a fake success


def test_the_token_is_stored_only_for_the_configured_account(tmp_path: Path) -> None:
    config = load_config(gmail_env(tmp_path, token=False), now=NOW)
    email, secrets = config.integrations.email, config.secrets.gmail
    wrong = FakeGmailApi(address="someone-else@ourco.example")
    with pytest.raises(GmailError) as error:
        gmail_provider.authorize_mailbox(email, secrets, flow=lambda c, s: fresh_credentials(), api_factory=lambda a, t: wrong)
    assert error.value.code is GmailCode.MAILBOX_MISMATCH and not email.token_file.exists()  # type: ignore[union-attr]
    address = gmail_provider.authorize_mailbox(email, secrets, flow=lambda c, s: fresh_credentials(),
                                               api_factory=lambda a, t: FakeGmailApi())
    assert address == email.address and email.token_file.exists()  # type: ignore[union-attr]


def run(argv: list[str], environ: dict[str, str]) -> tuple[int, dict[str, object], str]:
    out = io.StringIO()
    code = main(argv, environ, out)
    return code, json.loads(out.getvalue()), out.getvalue()


def test_gmail_auth_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gmail_auth, "run_installed_app_flow", lambda config, scopes: fresh_credentials())
    monkeypatch.setattr(gmail_provider, "real_api", lambda auth, timeout: FakeGmailApi())
    environ = gmail_env(tmp_path, token=False)
    code, report, text = run(["gmail-auth"], environ)
    assert (code, report) == (0, {"authorized": True, "mailbox": "sales@ourco.example"})
    assert not (tmp_path / "agent.sqlite3").exists()  # needs no database
    assert_clean(text)
    code, report, _ = run(["gmail-auth"], gmail_env(tmp_path, EMAIL_PROVIDER="none", GMAIL_ADDRESS=None,
                                                    GMAIL_TOKEN_FILE=None, GMAIL_CREDENTIALS_FILE=None))
    assert (code, report) == (2, {"error": "GMAIL_NOT_SELECTED"})


def test_gmail_auth_reports_an_incomplete_flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def headless(config: object, scopes: object) -> Credentials:
        raise OSError("no display")

    monkeypatch.setattr(gmail_auth, "run_installed_app_flow", headless)
    code, report, text = run(["gmail-auth"], gmail_env(tmp_path, token=False))
    assert (code, report) == (3, {"error": "GMAIL_AUTHORIZATION_FAILED", "code": "AUTHORIZATION_NOT_COMPLETED"})
    assert_clean(text)


@pytest.mark.parametrize("command", ["init", "health", "tick", "provider-status", "email-sync", "execution-pass"])
def test_no_other_command_ever_starts_the_oauth_flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("the interactive OAuth flow must only run from gmail-auth")

    monkeypatch.setattr(gmail_auth, "run_installed_app_flow", forbidden)
    monkeypatch.setattr(gmail_auth, "authorize", forbidden)
    environ = gmail_env(tmp_path, token=False)  # not authorized: the commands must fail closed instead
    out = io.StringIO()
    main([command], environ, out)
    assert "AssertionError" not in out.getvalue()
