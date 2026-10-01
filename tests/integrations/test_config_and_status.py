"""Provider configuration rules and configuration health: structural validity, secret
sources, NOT_IMPLEMENTED versus CONFIGURED, capability mapping, immutability and the
non-secret fingerprint."""

from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from app.integrations import (
    IntegrationConfig,
    LLMProviderConfig,
    LLMSecrets,
    ProviderCategory,
    ProviderSecrets,
    ProviderState,
    build_provider_adapters,
    evaluate,
    parse_integrations,
)
from app.integrations.status import REPO_ROOT
from tests.inbound.builders import MAILBOX
from tests.integrations.builders import API_KEY, BOT_TOKEN, gmail, llm, telegram

P, S = ProviderCategory, ProviderState


def status_of(values: dict[str, str | None], mailboxes: tuple[str, ...] = (MAILBOX,)):  # noqa: ANN201
    parsed = parse_integrations({k: v for k, v in values.items() if v is not None})
    return evaluate(parsed.config, parsed.secrets, mailboxes=mailboxes, extra=parsed.problems)


def test_no_provider_selected_is_valid_and_needs_nothing() -> None:
    status = status_of({})
    assert status.valid and [p.state for p in status.providers] == [S.DISABLED, S.DISABLED, S.DISABLED, S.CONFIGURED,
                                                                      S.DISABLED]
    assert status.of(P.KNOWLEDGE).capability_available  # the existing local index
    assert not status.production_ready and status.production_blockers == (
        "EMAIL:DISABLED", "LLM:DISABLED", "OPERATOR_CHANNEL:DISABLED")


@pytest.mark.parametrize("with_file", [False, True])
def test_gmail_structurally_valid_is_not_implemented_and_not_a_capability(tmp_path: Path, with_file: bool) -> None:
    status = status_of(gmail(tmp_path, with_file=with_file))
    email = status.of(P.EMAIL)
    assert (email.provider, email.state, email.configuration_valid) == ("GMAIL", S.NOT_IMPLEMENTED, True)
    assert not email.implemented and not email.capability_available and status.valid
    assert build_provider_adapters(parse_integrations(gmail(tmp_path)).config, ProviderSecrets()).email_transport is None


@pytest.mark.parametrize(("drop", "problem"), [
    ("GMAIL_CLIENT_ID", "SALES_AGENT_GMAIL_CLIENT_ID: MISSING_SECRET"),
    ("GMAIL_CLIENT_SECRET", "SALES_AGENT_GMAIL_CLIENT_SECRET: MISSING_SECRET"),
    ("GMAIL_TOKEN_FILE", "SALES_AGENT_GMAIL_TOKEN_FILE: MISSING_SETTING"),
    ("GMAIL_ADDRESS", "SALES_AGENT_GMAIL_ADDRESS: MISSING_SETTING"),
])
def test_gmail_missing_pieces_are_invalid(tmp_path: Path, drop: str, problem: str) -> None:
    email = status_of(gmail(tmp_path, **{drop: None})).of(P.EMAIL)
    assert email.state is S.INVALID and problem in email.problems


def test_gmail_credential_sources_and_files(tmp_path: Path) -> None:
    both = status_of(gmail(tmp_path, GMAIL_CREDENTIALS_FILE=str(tmp_path / "x.json"))).of(P.EMAIL)  # file and pair
    assert "SALES_AGENT_GMAIL_CREDENTIALS_FILE: SECRET_SOURCE_INVALID" in both.problems
    missing = status_of(gmail(tmp_path, with_file=True, GMAIL_CREDENTIALS_FILE=str(tmp_path / "absent.json"))).of(P.EMAIL)
    assert "SALES_AGENT_GMAIL_CREDENTIALS_FILE: CREDENTIAL_FILE_MISSING" in missing.problems
    nowhere = status_of(gmail(tmp_path, GMAIL_TOKEN_FILE=str(tmp_path / "no-dir" / "t.json"))).of(P.EMAIL)
    assert "SALES_AGENT_GMAIL_TOKEN_FILE: CREDENTIAL_FILE_MISSING" in nowhere.problems
    foreign = status_of(gmail(tmp_path, GMAIL_ADDRESS="other@ourco.example")).of(P.EMAIL)
    assert "SALES_AGENT_GMAIL_ADDRESS: INVALID_PROVIDER_CONFIG" in foreign.problems  # not one of our mailboxes


def test_credential_files_inside_the_code_tree_must_be_under_local(tmp_path: Path) -> None:
    tracked = REPO_ROOT / "app" / "gmail-token.json"
    email = status_of(gmail(tmp_path, GMAIL_TOKEN_FILE=str(tracked))).of(P.EMAIL)
    assert "SALES_AGENT_GMAIL_TOKEN_FILE: SECRET_SOURCE_INVALID" in email.problems
    local = REPO_ROOT / ".local" / "credentials" / "gmail-token.json"
    allowed = status_of(gmail(tmp_path, GMAIL_TOKEN_FILE=str(local))).of(P.EMAIL)
    assert "SALES_AGENT_GMAIL_TOKEN_FILE: SECRET_SOURCE_INVALID" not in allowed.problems


@pytest.mark.parametrize("provider", ["openai", "anthropic", "gemini"])
def test_every_llm_provider_is_configured_but_not_implemented(provider: str) -> None:
    ok = status_of(llm(provider)).of(P.LLM)
    assert (ok.provider, ok.state, ok.capability_available) == (provider.upper(), S.NOT_IMPLEMENTED, False)
    no_key = status_of(llm(provider, LLM_API_KEY=None)).of(P.LLM)
    assert no_key.state is S.INVALID and no_key.problems == ("SALES_AGENT_LLM_API_KEY: MISSING_SECRET",)
    no_model = status_of(llm(provider, LLM_MODEL=None)).of(P.LLM)
    assert no_model.problems == ("SALES_AGENT_LLM_MODEL: MISSING_SETTING",)
    parsed = parse_integrations({k: v for k, v in llm(provider).items() if v})
    assert build_provider_adapters(parsed.config, parsed.secrets).llm_transport is None  # no live client


def test_telegram_is_validated_structurally_and_not_implemented() -> None:
    ok = status_of(telegram()).of(P.OPERATOR_CHANNEL)
    assert (ok.state, ok.capability_available) == (S.NOT_IMPLEMENTED, False)
    for overrides, problem in [
        ({"TELEGRAM_BOT_TOKEN": None}, "SALES_AGENT_TELEGRAM_BOT_TOKEN: MISSING_SECRET"),
        ({"TELEGRAM_BOT_TOKEN": "not-a-bot-token"}, "SALES_AGENT_TELEGRAM_BOT_TOKEN: INVALID_PROVIDER_CONFIG"),
        ({"TELEGRAM_OPERATOR_CHAT_IDS": None}, "SALES_AGENT_TELEGRAM_OPERATOR_CHAT_IDS: MISSING_SETTING"),
        ({"TELEGRAM_OPERATOR_CHAT_IDS": "12,abc"}, "SALES_AGENT_TELEGRAM_OPERATOR_CHAT_IDS: INVALID_PROVIDER_CONFIG"),
        ({"TELEGRAM_OPERATOR_CHAT_IDS": "12,12"}, "SALES_AGENT_TELEGRAM_OPERATOR_CHAT_IDS: INVALID_PROVIDER_CONFIG"),
    ]:
        status = status_of(telegram(**overrides)).of(P.OPERATOR_CHANNEL)
        assert status.state is S.INVALID and problem in status.problems
        assert BOT_TOKEN not in status.model_dump_json()


def test_settings_or_secrets_for_an_unselected_provider_are_rejected(tmp_path: Path) -> None:
    status = status_of({"LLM_API_KEY": API_KEY, "TELEGRAM_OPERATOR_CHAT_IDS": "5", "GMAIL_ADDRESS": MAILBOX})
    assert status.of(P.LLM).problems == ("SALES_AGENT_LLM_API_KEY: PROVIDER_NOT_SELECTED",)
    assert status.of(P.OPERATOR_CHANNEL).problems == ("SALES_AGENT_TELEGRAM_OPERATOR_CHAT_IDS: PROVIDER_NOT_SELECTED",)
    assert status.of(P.EMAIL).problems == ("SALES_AGENT_GMAIL_ADDRESS: PROVIDER_NOT_SELECTED",)
    assert not status.valid


def test_unknown_providers_and_malformed_values() -> None:
    bad = status_of({"LLM_PROVIDER": "mistral", "EMAIL_PROVIDER": "smtp", "KNOWLEDGE_PROVIDER": "pinecone"})
    assert "SALES_AGENT_LLM_PROVIDER: UNKNOWN_PROVIDER" in bad.of(P.LLM).problems
    assert "SALES_AGENT_EMAIL_PROVIDER: UNKNOWN_PROVIDER" in bad.of(P.EMAIL).problems
    assert "SALES_AGENT_KNOWLEDGE_PROVIDER: UNKNOWN_PROVIDER" in bad.of(P.KNOWLEDGE).problems
    timeout = status_of(llm(LLM_TIMEOUT_SECONDS="0")).of(P.LLM)
    assert timeout.problems == ("SALES_AGENT_LLM_TIMEOUT_SECONDS: INVALID_PROVIDER_CONFIG",)
    knowledge = status_of({"KNOWLEDGE_DIR": "/definitely/not/here"}).of(P.KNOWLEDGE)
    assert knowledge.problems == ("SALES_AGENT_KNOWLEDGE_DIR: INVALID_PROVIDER_CONFIG",)


def test_configuration_is_immutable() -> None:
    config = IntegrationConfig(llm=LLMProviderConfig(provider="OPENAI", model="m"))  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        config.llm.model = "other"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        IntegrationConfig.model_validate({"llm": {"provider": "OPENAI", "api_key": "x"}})  # no secret fields here


def test_fingerprint_covers_settings_never_secrets(tmp_path: Path) -> None:
    first = parse_integrations({k: v for k, v in llm().items() if v})
    rotated = parse_integrations({k: v for k, v in llm(LLM_API_KEY="test-secret-do-not-use-rotated").items() if v})
    other_model = parse_integrations({k: v for k, v in llm(LLM_MODEL="another-model").items() if v})
    assert first.config.fingerprint() == rotated.config.fingerprint() != other_model.config.fingerprint()
    assert API_KEY not in first.config.model_dump_json() and "api_key" not in first.config.model_dump_json()
    secrets = ProviderSecrets(llm=LLMSecrets(api_key=SecretStr(API_KEY)))
    assert API_KEY not in evaluate(first.config, secrets).model_dump_json()
