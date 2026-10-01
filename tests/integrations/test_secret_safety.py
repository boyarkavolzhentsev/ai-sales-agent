"""Fake secrets never leak: not through reprs, serialization, validation errors, startup
errors, health, the CLI, logs or the database."""

import io
import logging
from pathlib import Path

import pytest
from pydantic import SecretStr

from app.integrations import GmailSecrets, LLMSecrets, ProviderSecrets, TelegramSecrets
from app.runtime import ConfigError, SalesAgentRuntime, StartupError, load_config
from app.runtime.cli import main
from tests.inbound.builders import NOW
from tests.integrations.builders import CREDENTIAL_CONTENT, FAKE_SECRETS, full_env

FILE_SECRET = "test-secret-do-not-use-file-content"


def assert_clean(text: str) -> None:
    for secret in (*FAKE_SECRETS, FILE_SECRET):
        assert secret not in text, f"leaked: {secret[:12]}..."


def test_secret_models_mask_every_rendering() -> None:
    secrets = ProviderSecrets(gmail=GmailSecrets(client_id=SecretStr(FAKE_SECRETS[0]), client_secret=SecretStr(FAKE_SECRETS[1]),
                                                 refresh_token=SecretStr(FAKE_SECRETS[2])),
                              llm=LLMSecrets(api_key=SecretStr(FAKE_SECRETS[3])),
                              telegram=TelegramSecrets(bot_token=SecretStr(FAKE_SECRETS[4])))
    for rendering in (repr(secrets), str(secrets), secrets.model_dump_json(), str(secrets.model_dump(mode="json")),
                      f"{secrets}", repr(secrets.gmail)):
        assert_clean(rendering)


def test_config_load_reports_and_errors_are_clean(tmp_path: Path) -> None:
    environ = full_env(tmp_path)
    config = load_config(environ, now=NOW)
    assert_clean(repr(config) + str(config) + config.model_dump_json())
    for broken in ({"MAX_SENDS_PER_DAY": "x"}, {"TELEGRAM_BOT_TOKEN": "bad-token-test-secret-do-not-use-api-key"},
                   {"GMAIL_CREDENTIALS_FILE": str(tmp_path / "c.json")}, {"LLM_TIMEOUT_SECONDS": "-1"}):
        with pytest.raises(ConfigError) as error:
            load_config(full_env(tmp_path, **broken), now=NOW)
        assert_clean(str(error.value) + repr(error.value) + repr(error.value.problems))
        assert "bad-token" not in str(error.value)


def test_startup_health_and_failures_are_clean(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    from tests.gmail.builders import connectors
    from tests.gmail.fakes import FakeGmailApi
    config = load_config(full_env(tmp_path), now=NOW)
    app = SalesAgentRuntime(config, connectors=connectors(FakeGmailApi()))
    report = app.start()
    assert_clean(report.model_dump_json() + app.health().model_dump_json() + repr(app.__dict__))
    app.stop()
    production = SalesAgentRuntime(load_config(full_env(tmp_path, MODE="production"), now=NOW))
    with pytest.raises(StartupError) as error:
        production.start()
    assert_clean(str(error.value) + repr(error.value) + production.health().model_dump_json())
    assert_clean(caplog.text)


@pytest.mark.parametrize("command", ["provider-status", "init", "health", "execution-metrics", "tick"])
def test_cli_output_is_clean(tmp_path: Path, command: str) -> None:
    for environ in (full_env(tmp_path), full_env(tmp_path, LLM_MODEL=None), full_env(tmp_path, MODE="production")):
        out = io.StringIO()
        main([command], environ, out)
        assert_clean(out.getvalue())


def test_credential_file_contents_are_never_read_or_printed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    environ = full_env(tmp_path, GMAIL_CLIENT_ID=None, GMAIL_CLIENT_SECRET=None, GMAIL_REFRESH_TOKEN=None,
                       GMAIL_CREDENTIALS_FILE=str(tmp_path / "credentials" / "client.json"))
    (tmp_path / "credentials").mkdir()
    (tmp_path / "credentials" / "client.json").write_text(CREDENTIAL_CONTENT, encoding="utf-8")
    opened: list[str] = []
    real_open = Path.open

    def spy(self: Path, *args: object, **kwargs: object):  # noqa: ANN202
        if self.name == "client.json":
            opened.append(str(self))
        return real_open(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "open", spy)
    out = io.StringIO()
    assert main(["provider-status"], environ, out) == 0
    assert opened == [] and FILE_SECRET not in out.getvalue()


def test_secrets_never_reach_the_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.runtime import cli
    from tests.gmail.builders import connectors
    from tests.gmail.fakes import FakeGmailApi
    monkeypatch.setattr(cli, "CONNECTORS", connectors(FakeGmailApi()))
    environ = full_env(tmp_path)
    assert main(["init"], environ, io.StringIO()) == 0
    assert main(["tick"], environ, io.StringIO()) == 0
    raw = (tmp_path / "agent.sqlite3").read_bytes().decode("latin-1")
    assert_clean(raw)
