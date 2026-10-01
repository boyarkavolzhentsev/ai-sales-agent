"""The (fake) bot token never leaks: not through reprs, results, status, health, the CLI,
logs, errors, Telegram messages or the database; customer text is excerpted and plain;
production stays NOT_READY (no LLM provider yet)."""

import io
import logging
from pathlib import Path

import pytest

from app.integrations.telegram.errors import TelegramCode, TelegramError
from app.runtime import SalesAgentRuntime, StartupError, cli, load_config
from tests.inbound.builders import NOW
from tests.runtime.builders import env
from tests.telegram.builders import BOT_TOKEN, console, fake_connectors, telegram_values
from tests.telegram.fakes import ALICE_CHAT, FakeTelegramApi
from tests.telegram.test_review import drafted

SECRET_PART = BOT_TOKEN.split(":", 1)[1]


def clean(text: str) -> None:
    assert BOT_TOKEN not in text and SECRET_PART not in text


def test_the_token_never_appears_in_runtime_renderings(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    c = console(tmp_path)
    drafted(c)
    c.telegram.text(ALICE_CHAT, "/status")
    result = c.sync()
    sync = c.app.services.operator_channel
    clean(result.model_dump_json() + repr(result) + c.app.startup_report.model_dump_json() if hasattr(c.app, "startup_report")
          else result.model_dump_json())
    clean(c.app.health().model_dump_json() + repr(c.app.__dict__) + repr(sync) + repr(c.app._adapters))  # noqa: SLF001
    clean("\n".join(s.text for s in c.telegram.sent) + repr(c.telegram.sent))
    [status_text] = [t for t in c.telegram.texts(ALICE_CHAT) if t.startswith("Runtime:")]
    assert "OPERATOR_CHANNEL" in status_text and "CONFIGURED" in status_text
    clean(caplog.text)
    c.app.stop()
    raw = (tmp_path / "agent.sqlite3").read_bytes()
    assert BOT_TOKEN.encode() not in raw and SECRET_PART.encode() not in raw


def test_startup_and_sync_failures_are_codes_only(tmp_path: Path) -> None:
    api = FakeTelegramApi(fail={"get_me": TelegramError(TelegramCode.AUTH_INVALID, status=401)})
    app = SalesAgentRuntime(load_config(env(tmp_path / "x.sqlite3", **telegram_values()), now=NOW), connectors=fake_connectors(api))
    with pytest.raises(StartupError) as error:
        app.start()
    assert "AUTH_INVALID" in str(error.value)
    clean(str(error.value) + repr(error.value) + app.health().model_dump_json())


def test_the_cli_never_prints_the_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    api = FakeTelegramApi()
    monkeypatch.setattr(cli, "CONNECTORS", fake_connectors(api))
    environ = env(tmp_path / "cli.sqlite3", **telegram_values())
    out = io.StringIO()
    for command in (["provider-status"], ["init"], ["health"], ["operator-sync"]):
        cli.main(command, environ, out)
    api.fail["get_me"] = TelegramError(TelegramCode.AUTH_INVALID, status=401)
    cli.main(["operator-sync"], environ, out)
    text = out.getvalue()
    assert "STARTUP_FAILED" in text and "OPERATOR_CHANNEL_PROVIDER_UNAVAILABLE" in text  # codes only
    clean(text)


def test_production_is_not_ready_without_an_llm(tmp_path: Path) -> None:
    from tests.integrations.builders import NO_LLM, full_env
    app = SalesAgentRuntime(load_config(full_env(tmp_path, MODE="production", **NO_LLM), now=NOW), connectors=fake_connectors())
    with pytest.raises(StartupError) as error:
        app.start()
    assert str(error.value) == "PRODUCTION_NOT_READY: LLM:DISABLED"


def test_customer_text_in_cards_is_plain_cleaned_and_bounded(tmp_path: Path) -> None:
    from app.integrations.telegram.rendering import MAX_MESSAGE, TRUNCATED
    from tests.orchestration.builders import approve_pending, customer_replies, enrolled
    c = console(tmp_path)
    enrolled(c.world)
    c.world.execute()
    approve_pending(c.world)
    c.world.execute(dispatch=True)
    hostile = ("How much is the Basic plan per month? ‮<a href='https://evil.example'>click</a> *bold* "
               + "x" * 5000)
    customer_replies(c.world, "p-hostile", body=hostile)
    c.sync()
    card = [s for s in c.telegram.sent if s.chat_id == ALICE_CHAT and s.text.startswith("Reply draft")][-1]
    assert len(card.text) <= MAX_MESSAGE and TRUNCATED in card.text and "‮" not in card.text
    assert "<a href='https://evil.example'>click</a>" in card.text  # inert: no parse mode is ever used
    assert all(label in ("Approve", "Reject", "Lost…", "Do not contact…") for row in card.buttons for label, _ in row)
