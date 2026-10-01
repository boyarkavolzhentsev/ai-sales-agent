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
from tests.integrations.builders import API_KEY, BOT_TOKEN, CLIENT_ID, full_env, llm, telegram
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
    assert config.integrations.email.provider.value == "GMAIL" and config.integrations.operator.operator_chat_ids == (1001, -2002)
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
    config = load_config(full_env(tmp_path, MODE="production"), now=NOW)
    assert config.mode is RuntimeMode.PRODUCTION
    app = SalesAgentRuntime(config)
    with pytest.raises(StartupError) as error:
        app.start()
    assert error.value.code == "PRODUCTION_NOT_READY" and app.state is RuntimeState.FAILED
    assert str(error.value) == "PRODUCTION_NOT_READY: EMAIL:NOT_IMPLEMENTED, LLM:NOT_IMPLEMENTED, OPERATOR_CHANNEL:NOT_IMPLEMENTED"
    assert not (tmp_path / "agent.sqlite3").exists()  # refused before touching the database


# ---- Runtime -----------------------------------------------------------------------------------------


def test_selected_but_unimplemented_providers_never_become_capabilities(tmp_path: Path) -> None:
    app = SalesAgentRuntime(load_config(full_env(tmp_path), now=NOW))
    report = app.start()
    assert (report.capabilities.dispatch, report.capabilities.reconciliation, report.capabilities.inbound) == (False, False, False)
    states = {p.category: p.state for p in report.integrations.providers}
    assert states == {P.EMAIL: S.NOT_IMPLEMENTED, P.LLM: S.NOT_IMPLEMENTED, P.OPERATOR_CHANNEL: S.NOT_IMPLEMENTED,
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
                                                 | telegram(TELEGRAM_OPERATOR_CHAT_IDS="77"))), now=NOW)
    assert a.integrations != b.integrations and a.integrations.fingerprint() != b.integrations.fingerprint()
    assert a.secrets.llm.api_key is not None and b.secrets.llm.api_key is not None
    assert a.secrets.llm.api_key.get_secret_value() != b.secrets.llm.api_key.get_secret_value()
    first, second = SalesAgentRuntime(a), SalesAgentRuntime(b)
    report_a, report_b = first.start(), second.start()
    assert report_a.integrations.of(P.OPERATOR_CHANNEL).state is S.DISABLED
    assert report_b.integrations.of(P.OPERATOR_CHANNEL).state is S.NOT_IMPLEMENTED
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
    code, report, _ = run("provider-status", environ=full_env(tmp_path))
    assert code == OK and report["configuration_valid"] is True and report["mode"] == "LOCAL"
    assert report["production_ready"] is False and not (tmp_path / "agent.sqlite3").exists()
    integrations = report["integrations"]
    assert isinstance(integrations, dict)
    rows = {row["category"]: row for row in integrations["providers"]}  # type: ignore[index]
    assert (rows["EMAIL"]["provider"], rows["EMAIL"]["state"], rows["EMAIL"]["capability_available"]) == (
        "GMAIL", "NOT_IMPLEMENTED", False)


def test_provider_status_names_invalid_configuration(tmp_path: Path) -> None:
    code, report, text = run("provider-status", environ=full_env(tmp_path, LLM_API_KEY=None, MAX_SENDS_PER_DAY="x"))
    assert code == INVALID_CONFIG and report["configuration_valid"] is False
    assert "SALES_AGENT_LLM_API_KEY: MISSING_SECRET" in report["problems"]  # type: ignore[operator]
    assert "SALES_AGENT_MAX_SENDS_PER_DAY: must be a whole number >= 0" in report["problems"]  # type: ignore[operator]
    assert CLIENT_ID not in text and BOT_TOKEN not in text


def test_runtime_commands_refuse_production_before_anything_else(tmp_path: Path) -> None:
    environ = full_env(tmp_path, MODE="production")
    code, report, _ = run("init", environ=environ)
    assert (code, report["error"], report["code"]) == (3, "STARTUP_FAILED", "PRODUCTION_NOT_READY")
    assert not (tmp_path / "agent.sqlite3").exists()
