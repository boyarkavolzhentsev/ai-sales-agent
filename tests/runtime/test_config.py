"""Configuration: explicit values, environment parsing, validation and secret safety."""

from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from app.persistence import MEMORY
from app.policy import Weekday
from app.integrations import LLMSecrets
from app.runtime import ConfigError, ProviderSecrets, RuntimeMode, load_config
from tests.inbound.builders import NOW
from tests.runtime.builders import env, runtime_config

SECRET = "sk-live-do-not-print-4f2a"


def test_explicit_config_builds_every_subsystem_contract(tmp_path: Path) -> None:
    config = runtime_config(tmp_path / "db.sqlite3")
    dispatch, follow_up, campaign = config.dispatch_config(), config.follow_up_config(), config.campaign_config()
    assert dispatch.sender_mailboxes == config.mailboxes and dispatch.limits == config.limits == follow_up.limits == campaign.limits
    assert config.inbound_config().own_addresses == config.mailboxes and config.operator_config().authorized_operator_ids == config.operator_ids
    assert follow_up.window == campaign.window == dispatch.window == config.window


def test_environment_config_is_parsed_explicitly(tmp_path: Path) -> None:
    config = load_config(env(tmp_path / "db.sqlite3", BATCH_LIMIT="7", WORKER_ID="w-1"), now=NOW)
    assert config.mode is RuntimeMode.LOCAL and config.worker.batch_limit == 7 and config.worker.worker_id == "w-1"
    assert config.window.working_days == (Weekday.MONDAY, Weekday.TUESDAY, Weekday.WEDNESDAY, Weekday.THURSDAY, Weekday.FRIDAY)
    assert config.limits.timezone == "Europe/Kyiv" and not config.kill_switch.enabled and config.kill_switch.changed_at == NOW


@pytest.mark.parametrize("missing", ["DATABASE_PATH", "MODE", "MAX_SENDS_PER_DAY", "KILL_SWITCH", "WINDOW_DAYS", "OPERATOR_IDS"])
def test_every_required_value_must_be_present(tmp_path: Path, missing: str) -> None:
    with pytest.raises(ConfigError) as error:
        load_config(env(tmp_path / "db.sqlite3", **{missing: None}), now=NOW)
    assert f"SALES_AGENT_{missing}: required" in error.value.problems


@pytest.mark.parametrize(
    ("name", "value"),
    [("KILL_SWITCH", "yes"), ("KILL_SWITCH", "0"), ("MAX_SENDS_PER_DAY", "-1"), ("MAX_SENDS_PER_DAY", "ten"),
     ("BATCH_LIMIT", "0"), ("TIMEZONE", "Mars/Olympus"), ("WINDOW_DAYS", "MON,FUNDAY"), ("WINDOW_START", "9am"),
     ("MODE", "staging"), ("MIN_FOLLOW_UP_INTERVAL_HOURS", "0")],
)
def test_malformed_values_fail_with_the_variable_named(tmp_path: Path, name: str, value: str) -> None:
    with pytest.raises(ConfigError) as error:
        load_config(env(tmp_path / "db.sqlite3", **{name: value}), now=NOW)
    text = str(error.value)
    assert (f"SALES_AGENT_{name}" in text) or (name == "TIMEZONE" and "time zone" in text)


def test_an_invalid_kill_switch_never_reads_as_off(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(env(tmp_path / "db.sqlite3", KILL_SWITCH="off"), now=NOW)
    with pytest.raises(ConfigError) as error:
        load_config(env(tmp_path / "db.sqlite3", KILL_SWITCH="true"), now=NOW)  # on, but no reason given
    assert "SALES_AGENT_KILL_SWITCH_REASON: required when the kill switch is on" in error.value.problems
    on = load_config(env(tmp_path / "db.sqlite3", KILL_SWITCH="true", KILL_SWITCH_REASON="incident"), now=NOW)
    assert on.kill_switch.enabled


def test_unknown_variables_and_dangerous_database_paths_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="SALES_AGENT_MAX_SEND_PER_DAY: unknown variable"):
        load_config(env(tmp_path / "db.sqlite3", MAX_SEND_PER_DAY="1000"), now=NOW)  # typo
    with pytest.raises(ConfigError, match="is a directory"):
        load_config(env(tmp_path), now=NOW)
    with pytest.raises(ConfigError, match="parent directory does not exist"):
        load_config(env(tmp_path / "missing" / "db.sqlite3"), now=NOW)
    with pytest.raises(ConfigError, match="only allowed in test mode"):
        load_config(env(MEMORY), now=NOW)
    assert load_config(env(MEMORY, MODE="test"), now=NOW).database_path == MEMORY
    with pytest.raises(ValidationError, match="only allowed in TEST mode"):
        runtime_config(MEMORY)


def test_secrets_never_appear_in_reprs_or_errors(tmp_path: Path) -> None:
    selected = {"LLM_PROVIDER": "openai", "LLM_MODEL": "model-x", "LLM_API_KEY": SECRET}
    config = load_config(env(tmp_path / "db.sqlite3", **selected), now=NOW)
    assert config.secrets.llm.api_key is not None and config.secrets.llm.api_key.get_secret_value() == SECRET
    for rendering in (repr(config), str(config), config.model_dump_json(), repr(config.secrets)):
        assert SECRET not in rendering
    with pytest.raises(ConfigError) as error:
        load_config(env(tmp_path / "db.sqlite3", **selected, MAX_SENDS_PER_DAY="x"), now=NOW)
    assert SECRET not in str(error.value) and SECRET not in repr(error.value)
    assert "sk-" not in repr(ProviderSecrets(llm=LLMSecrets(api_key=SecretStr(SECRET))))


def test_no_provider_credential_is_required(tmp_path: Path) -> None:
    config = load_config(env(tmp_path / "db.sqlite3"), now=NOW)
    assert config.secrets == ProviderSecrets()
