"""Provider variables through the existing SALES_AGENT_* loader, runtime startup (offline,
not-implemented, production), the provider-status CLI and deployment isolation."""

import io
import json
from pathlib import Path

import pytest

from app.integrations import ProviderCategory, ProviderSecrets, ProviderState
from app.runtime import ConfigError, RuntimeMode, SalesAgentRuntime, StartupError, load_config
from app.runtime.cli import INVALID_CONFIG, OK, main
from app.runtime.results import RuntimeState
from tests.inbound.builders import NOW
from tests.integrations.builders import API_KEY, BOT_TOKEN, CLIENT_ID, embeddings, full_env, llm, telegram
from tests.runtime.builders import env

P, S = ProviderCategory, ProviderState


# ---- Environment ----------------------------------------------------------------------------------


def test_no_provider_variables_keep_the_offline_default(tmp_path: Path) -> None:
    config = load_config(env(tmp_path / "db.sqlite3"), now=NOW)
    assert config.secrets == ProviderSecrets()
    assert [s.value for s in (config.integrations.email.provider, config.integrations.llm.provider,
                               config.integrations.operator.provider)] == ["NONE", "NONE", "NONE"]


def test_every_provider_selected_with_fake_secrets_loads(tmp_path: Path) -> None:
    config = load_config(full_env(tmp_path), now=NOW)
    assert config.integrations.email.provider.value == "GMAIL" and [(o.chat_id, o.operator_id) for o in config.integrations.operator.operators] == [(1001, "op-alice"), (2002, "op-bob")]
    assert config.secrets.gmail.client_id is not None and config.secrets.gmail.client_id.get_secret_value() == CLIENT_ID
    assert config.secrets.telegram.bot_token is not None and config.secrets.llm.api_key is not None


@pytest.mark.parametrize(("overrides", "expected"), [
    ({"LLM_PROVIDER": "mistral"}, "SALES_AGENT_LLM_PROVIDER: UNKNOWN_PROVIDER"),
    ({"LLM_PROVIDER": "openai", "LLM_MODEL": "m"}, "SALES_AGENT_LLM_API_KEY: MISSING_SECRET"),
    ({"TELEGRAM_BOT_TOKEN": BOT_TOKEN}, "SALES_AGENT_TELEGRAM_BOT_TOKEN: PROVIDER_NOT_SELECTED"),
    ({"GMAIL_API_TOKEN": "x"}, "SALES_AGENT_GMAIL_API_TOKEN: unknown variable"),
    ({"EMAIL_API_TOKEN": "x"}, "SALES_AGENT_EMAIL_API_TOKEN: unknown variable"),  # the Stage 11 placeholder is gone
    ({"EMAIL_PROVIDER": "gmail"}, "SALES_AGENT_GMAIL_ADDRESS: MISSING_SETTING"),
])
def test_invalid_provider_variables_fail_loading_by_name(tmp_path: Path, overrides: dict[str, str], expected: str) -> None:
    with pytest.raises(ConfigError) as error:
        load_config(env(tmp_path / "db.sqlite3", **overrides), now=NOW)
    assert expected in error.value.problems


def test_an_unknown_provider_reports_only_itself(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as error:
        load_config(env(tmp_path / "db.sqlite3", LLM_PROVIDER="mistral", LLM_API_KEY=API_KEY), now=NOW)
    assert error.value.problems == ("SALES_AGENT_LLM_PROVIDER: UNKNOWN_PROVIDER",)


def test_production_is_a_mode_and_it_fails_closed(tmp_path: Path) -> None:
    from tests.integrations.builders import NO_LLM
    config = load_config(full_env(tmp_path, MODE="production", **(NO_LLM | embeddings())), now=NOW)
    assert config.mode is RuntimeMode.PRODUCTION
    app = SalesAgentRuntime(config)
    with pytest.raises(StartupError) as error:
        app.start()
    assert error.value.code == "PRODUCTION_NOT_READY" and app.state is RuntimeState.FAILED
    # Every required category must be CONFIGURED: without an LLM production fails closed.
    assert str(error.value) == "PRODUCTION_NOT_READY: LLM:DISABLED"
    assert not (tmp_path / "agent.sqlite3").exists()  # refused before touching the database


def test_production_starts_only_when_every_required_provider_is_configured(tmp_path: Path) -> None:
    # Stage 18: Gmail, Telegram, an LLM and LOCAL knowledge are all implemented; Stage 19 adds
    # embeddings (semantic retrieval). With every one configured (and Gmail/Telegram verified at
    # startup) production starts.
    from tests.telegram.builders import fake_connectors
    app = SalesAgentRuntime(load_config(full_env(tmp_path, MODE="production", **embeddings()), now=NOW),
                            connectors=fake_connectors())
    report = app.start()
    assert report.integrations.production_ready and report.integrations.production_blockers == ()
    assert app.state is RuntimeState.READY
    app.stop()


# ---- Runtime -----------------------------------------------------------------------------------------


def test_selected_providers_become_capabilities(tmp_path: Path) -> None:
    from tests.telegram.builders import fake_connectors
    app = SalesAgentRuntime(load_config(full_env(tmp_path), now=NOW), connectors=fake_connectors())
    report = app.start()
    # Gmail (Stage 16), Telegram (Stage 17) and the LLM (Stage 18) are capabilities.
    assert (report.capabilities.dispatch, report.capabilities.reconciliation, report.capabilities.inbound) == (True, True, True)
    assert report.capabilities.operator_channel and report.capabilities.qualification_extraction
    assert report.capabilities.commercial_extraction and report.capabilities.sales_advice
    states = {p.category: p.state for p in report.integrations.providers}
    assert states == {P.EMAIL: S.CONFIGURED, P.LLM: S.CONFIGURED, P.OPERATOR_CHANNEL: S.CONFIGURED,
                      P.KNOWLEDGE: S.CONFIGURED, P.EMBEDDINGS: S.DISABLED}
    assert app.health().integrations == report.integrations and app.health().ready
    assert app.campaign_tick().status.value == "OK"  # the offline runtime works exactly as before
    app.stop()


def test_a_programmatic_config_with_invalid_providers_does_not_start(tmp_path: Path) -> None:
    from app.integrations import IntegrationConfig, LLMProviderConfig
    from tests.runtime.builders import runtime_config
    config = runtime_config(tmp_path / "db.sqlite3", integrations=IntegrationConfig(
        llm=LLMProviderConfig(provider="OPENAI", model="m")))  # type: ignore[arg-type] - no API key
    app = SalesAgentRuntime(config)
    with pytest.raises(StartupError) as error:
        app.start()
    assert error.value.code == "INTEGRATION_CONFIG_INVALID" and "SALES_AGENT_LLM_API_KEY: MISSING_SECRET" in str(error.value)


def test_deployments_are_isolated(tmp_path: Path) -> None:
    a_dir, b_dir = tmp_path / "a", tmp_path / "b"
    a_dir.mkdir()
    b_dir.mkdir()
    a = load_config(env(a_dir / "a.sqlite3", **llm("openai", LLM_API_KEY="test-secret-do-not-use-tenant-a")), now=NOW)
    b = load_config(env(b_dir / "b.sqlite3", **(llm("anthropic", LLM_API_KEY="test-secret-do-not-use-tenant-b")
                                                 | telegram(TELEGRAM_OPERATOR_CHAT_IDS="77=op-bob"))), now=NOW)
    assert a.integrations != b.integrations and a.integrations.fingerprint() != b.integrations.fingerprint()
    assert a.secrets.llm.api_key is not None and b.secrets.llm.api_key is not None
    assert a.secrets.llm.api_key.get_secret_value() != b.secrets.llm.api_key.get_secret_value()
    from tests.telegram.builders import fake_connectors
    first, second = SalesAgentRuntime(a), SalesAgentRuntime(b, connectors=fake_connectors())
    report_a, report_b = first.start(), second.start()
    assert report_a.integrations.of(P.OPERATOR_CHANNEL).state is S.DISABLED
    assert report_b.integrations.of(P.OPERATOR_CHANNEL).state is S.CONFIGURED
    assert "tenant-b" not in repr(first.__dict__) and "tenant-a" not in repr(second.__dict__)
    first.stop()
    second.stop()
    assert load_config(env(a_dir / "a.sqlite3", **llm("openai", LLM_API_KEY="test-secret-do-not-use-tenant-a")), now=NOW) == a


# ---- CLI ------------------------------------------------------------------------------------------------


def run(*argv: str, environ: dict[str, str]) -> tuple[int, dict[str, object], str]:
    out = io.StringIO()
    code = main(list(argv), environ, out)
    return code, json.loads(out.getvalue()), out.getvalue()


def test_provider_status_reports_without_a_database(tmp_path: Path) -> None:
    code, report, _ = run("provider-status", environ=full_env(tmp_path, **embeddings()))
    assert code == OK and report["configuration_valid"] is True and report["mode"] == "LOCAL"
    # Every required category is configured (Stages 18/19); provider-status itself contacts nothing.
    assert report["production_ready"] is True and not (tmp_path / "agent.sqlite3").exists()
    integrations = report["integrations"]
    assert isinstance(integrations, dict)
    rows = {row["category"]: row for row in integrations["providers"]}  # type: ignore[index]
    assert (rows["EMAIL"]["provider"], rows["EMAIL"]["state"], rows["EMAIL"]["authorization"]) == (
        "GMAIL", "CONFIGURED", "AUTHORIZED")  # locally: the refresh secret; nothing was contacted
    assert (rows["LLM"]["state"], rows["LLM"]["capability_available"]) == ("CONFIGURED", True)


def test_provider_status_names_invalid_configuration(tmp_path: Path) -> None:
    code, report, text = run("provider-status", environ=full_env(tmp_path, LLM_API_KEY=None, MAX_SENDS_PER_DAY="x"))
    assert code == INVALID_CONFIG and report["configuration_valid"] is False
    assert "SALES_AGENT_LLM_API_KEY: MISSING_SECRET" in report["problems"]  # type: ignore[operator]
    assert "SALES_AGENT_MAX_SENDS_PER_DAY: must be a whole number >= 0" in report["problems"]  # type: ignore[operator]
    assert CLIENT_ID not in text and BOT_TOKEN not in text


def test_runtime_commands_refuse_production_before_anything_else(tmp_path: Path) -> None:
    from tests.integrations.builders import NO_LLM
    environ = full_env(tmp_path, MODE="production", **NO_LLM)
    code, report, _ = run("init", environ=environ)
    assert (code, report["error"], report["code"]) == (3, "STARTUP_FAILED", "PRODUCTION_NOT_READY")
    assert not (tmp_path / "agent.sqlite3").exists()
