"""Configuration health of every provider: structural validity, credential sources and
implementation availability. Local checks only (field presence, formats, file metadata):
no network, no provider call, and no credential file is ever read.

States:
  DISABLED         provider NONE (nothing configured; nothing required)
  INVALID          selected (or not selected) with missing, malformed or contradictory
                   settings/secrets; problems name each variable and a code
  NOT_IMPLEMENTED  valid configuration, but no adapter exists yet: the capability stays
                   unavailable
  AUTH_REQUIRED    valid configuration and an implementation, but no usable local
                   authorization (Gmail without a usable token): run the provider's explicit
                   authorization command; the capability stays unavailable
  CONFIGURED       valid configuration, an implementation and (where needed) a local
                   authorization: usable. Connectivity is checked only when the runtime
                   builds the adapters, never here.

A production deployment is ready only when every required category is CONFIGURED.
"""

import os
import re
import stat
from collections.abc import Iterable
from enum import StrEnum
from pathlib import Path

from app.core.models.base import CoreModel
from app.integrations.config import IntegrationConfig
from app.integrations.errors import IntegrationCode, IntegrationProblem
from app.integrations.providers import (
    EmailProviderId,
    LLMProviderId,
    OperatorProviderId,
    ProviderCategory,
)
from app.integrations.registry import is_implemented, selected
from app.integrations.secrets import ProviderSecrets

C, P = IntegrationCode, ProviderCategory
# The code tree. Credential files may live inside it only under ``.local/`` (gitignored).
REPO_ROOT = Path(__file__).resolve().parents[2]
LOCAL_DIR = REPO_ROOT / ".local"
PRODUCTION_REQUIRED = (P.EMAIL, P.LLM, P.OPERATOR_CHANNEL, P.KNOWLEDGE)
# A Telegram bot token's shape (<bot id>:<secret>); the value itself is never reported.
TELEGRAM_TOKEN = re.compile(r"^\d{3,}:[A-Za-z0-9_-]{20,}$")

# Model field -> SALES_AGENT_* variable (without the prefix).
VARIABLES: dict[tuple[str, str], str] = {
    ("email", "provider"): "EMAIL_PROVIDER", ("email", "address"): "GMAIL_ADDRESS",
    ("email", "credentials_file"): "GMAIL_CREDENTIALS_FILE", ("email", "token_file"): "GMAIL_TOKEN_FILE",
    ("email", "poll_interval_seconds"): "GMAIL_POLL_INTERVAL_SECONDS", ("email", "timeout_seconds"): "GMAIL_TIMEOUT_SECONDS",
    ("llm", "provider"): "LLM_PROVIDER", ("llm", "model"): "LLM_MODEL", ("llm", "timeout_seconds"): "LLM_TIMEOUT_SECONDS",
    ("llm", "max_output_tokens"): "LLM_MAX_OUTPUT_TOKENS",
    ("operator", "provider"): "OPERATOR_PROVIDER", ("operator", "operators"): "TELEGRAM_OPERATOR_CHAT_IDS",
    ("operator", "timeout_seconds"): "TELEGRAM_TIMEOUT_SECONDS",
    ("knowledge", "provider"): "KNOWLEDGE_PROVIDER", ("knowledge", "directory"): "KNOWLEDGE_DIR",
    ("embeddings", "provider"): "EMBEDDINGS_PROVIDER",
}
SECRET_VARIABLES: dict[tuple[str, str], str] = {
    ("gmail", "client_id"): "GMAIL_CLIENT_ID", ("gmail", "client_secret"): "GMAIL_CLIENT_SECRET",
    ("gmail", "refresh_token"): "GMAIL_REFRESH_TOKEN", ("llm", "api_key"): "LLM_API_KEY",
    ("telegram", "bot_token"): "TELEGRAM_BOT_TOKEN",
}
CATEGORY_OF_SECTION = {"email": P.EMAIL, "llm": P.LLM, "operator": P.OPERATOR_CHANNEL, "knowledge": P.KNOWLEDGE,
                       "embeddings": P.EMBEDDINGS, "gmail": P.EMAIL, "telegram": P.OPERATOR_CHANNEL}


class ProviderState(StrEnum):
    DISABLED = "DISABLED"
    INVALID = "INVALID"
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    CONFIGURED = "CONFIGURED"


class ProviderStatus(CoreModel):
    """Sanitized: provider IDs, states and codes; never a value, token or path."""

    category: ProviderCategory
    provider: str
    state: ProviderState
    configuration_valid: bool
    implemented: bool
    # Local authorization where the provider needs one (Gmail): AUTHORIZED, AUTH_REQUIRED
    # or AUTH_INVALID. Telegram: TOKEN_PRESENT (structurally valid, not verified here; the
    # runtime verifies it with one getMe at startup). None when not applicable.
    authorization: str | None = None
    capability_available: bool
    problems: tuple[str, ...] = ()  # "SALES_AGENT_<VAR>: <CODE>"
    warnings: tuple[str, ...] = ()


class IntegrationStatus(CoreModel):
    providers: tuple[ProviderStatus, ...]
    valid: bool
    production_ready: bool
    production_blockers: tuple[str, ...] = ()  # "<CATEGORY>:<STATE>"
    fingerprint: str

    def of(self, category: ProviderCategory) -> ProviderStatus:
        return next(p for p in self.providers if p.category is category)


def evaluate(config: IntegrationConfig, secrets: ProviderSecrets, *, mailboxes: Iterable[str] = (),
             operator_ids: Iterable[str] = (), extra: Iterable[IntegrationProblem] = ()) -> IntegrationStatus:
    """``extra``: problems found while parsing (e.g. an unknown provider ID)."""
    extra = tuple(extra)
    unknown = {p.category for p in extra if p.code is C.UNKNOWN_PROVIDER}
    # With an unknown provider ID nothing is "not selected": only the unknown ID is reported.
    problems: list[IntegrationProblem] = [*extra, *(p for p in _unselected(config, secrets) if p.category not in unknown)]
    warnings: list[IntegrationProblem] = []
    _email(config, secrets, tuple(mailboxes), problems, warnings)
    _llm(config, secrets, problems)
    _operator(config, secrets, tuple(operator_ids), problems)
    _knowledge(config, problems)
    statuses = []
    for category, provider in selected(config):
        own = tuple(dict.fromkeys(p.render() for p in problems if p.category is category))
        implemented = is_implemented(category, provider)
        authorization = _authorization(config, secrets) if category is P.EMAIL and not own else None
        if own:
            state = ProviderState.INVALID
        elif provider == "NONE":
            state = ProviderState.DISABLED
        elif not implemented:
            state = ProviderState.NOT_IMPLEMENTED
        elif authorization not in (None, "AUTHORIZED"):
            state = ProviderState.AUTH_REQUIRED
            code = C.AUTH_REQUIRED if authorization == "AUTH_REQUIRED" else C.AUTH_INVALID
            warnings.append(_problem(category, code, "GMAIL_TOKEN_FILE"))
        else:
            state = ProviderState.CONFIGURED
            if category is P.OPERATOR_CHANNEL:
                authorization = "TOKEN_PRESENT"
        statuses.append(ProviderStatus(
            category=category, provider=provider, state=state, configuration_valid=not own, implemented=implemented,
            authorization=authorization, capability_available=state is ProviderState.CONFIGURED, problems=own,
            warnings=tuple(dict.fromkeys(w.render() for w in warnings if w.category is category))))
    blockers = tuple(f"{s.category.value}:{s.state.value}" for s in statuses
                     if s.category in PRODUCTION_REQUIRED and s.state is not ProviderState.CONFIGURED)
    valid = all(s.configuration_valid for s in statuses)
    return IntegrationStatus(providers=tuple(statuses), valid=valid, production_ready=valid and not blockers,
                             production_blockers=blockers, fingerprint=config.fingerprint())


def _authorization(config: IntegrationConfig, secrets: ProviderSecrets) -> str | None:
    """Gmail's local authorization (token file / refresh secret): no network, no Google
    library, and the OAuth client file is not read."""
    if config.email.provider is not EmailProviderId.GMAIL or config.email.token_file is None:
        return None
    from app.integrations.gmail.tokens import local_authorization

    return local_authorization(config.email.token_file, refresh_secret=secrets.gmail.refresh_token is not None).value


def _problem(category: ProviderCategory, code: IntegrationCode, variable: str) -> IntegrationProblem:
    return IntegrationProblem(category=category, code=code, variable=variable)


def _unselected(config: IntegrationConfig, secrets: ProviderSecrets) -> list[IntegrationProblem]:
    """Settings or secrets for a provider that is not selected are contradictory: rejected,
    never silently ignored."""
    found: list[IntegrationProblem] = []
    for section, model in (("email", config.email), ("llm", config.llm), ("operator", config.operator)):
        if model.provider.value != "NONE":
            continue
        for name in sorted(model.model_fields_set - {"provider"}):
            found.append(_problem(CATEGORY_OF_SECTION[section], C.PROVIDER_NOT_SELECTED, VARIABLES[(section, name)]))
    owners = {"gmail": config.email.provider is not EmailProviderId.GMAIL,
              "llm": config.llm.provider is LLMProviderId.NONE,
              "telegram": config.operator.provider is not OperatorProviderId.TELEGRAM}
    for (section, name), variable in SECRET_VARIABLES.items():
        if owners[section] and getattr(getattr(secrets, section), name) is not None:
            found.append(_problem(CATEGORY_OF_SECTION[section], C.PROVIDER_NOT_SELECTED, variable))
    return found


def _email(config: IntegrationConfig, secrets: ProviderSecrets, mailboxes: tuple[str, ...],
           problems: list[IntegrationProblem], warnings: list[IntegrationProblem]) -> None:
    email, gmail = config.email, secrets.gmail
    if email.provider is not EmailProviderId.GMAIL:
        return
    if email.address is None:
        problems.append(_problem(P.EMAIL, C.MISSING_SETTING, "GMAIL_ADDRESS"))
    elif mailboxes and email.address not in mailboxes:
        problems.append(_problem(P.EMAIL, C.INVALID_PROVIDER_CONFIG, "GMAIL_ADDRESS"))  # not one of our mailboxes
    if email.token_file is None:
        problems.append(_problem(P.EMAIL, C.MISSING_SETTING, "GMAIL_TOKEN_FILE"))
    else:
        _token_file(email.token_file, problems)
    # The OAuth client comes from exactly one source: a local client file, or the id/secret pair.
    pair = gmail.client_id is not None or gmail.client_secret is not None
    if email.credentials_file is not None and pair:
        problems.append(_problem(P.EMAIL, C.SECRET_SOURCE_INVALID, "GMAIL_CREDENTIALS_FILE"))
    elif email.credentials_file is not None:
        _credential_file(email.credentials_file, "GMAIL_CREDENTIALS_FILE", problems, warnings)
    else:
        if gmail.client_id is None:
            problems.append(_problem(P.EMAIL, C.MISSING_SECRET, "GMAIL_CLIENT_ID"))
        if gmail.client_secret is None:
            problems.append(_problem(P.EMAIL, C.MISSING_SECRET, "GMAIL_CLIENT_SECRET"))


def _llm(config: IntegrationConfig, secrets: ProviderSecrets, problems: list[IntegrationProblem]) -> None:
    if config.llm.provider is LLMProviderId.NONE:
        return
    if config.llm.model is None:
        problems.append(_problem(P.LLM, C.MISSING_SETTING, "LLM_MODEL"))
    if secrets.llm.api_key is None:
        problems.append(_problem(P.LLM, C.MISSING_SECRET, "LLM_API_KEY"))


def _operator(config: IntegrationConfig, secrets: ProviderSecrets, operator_ids: tuple[str, ...],
              problems: list[IntegrationProblem]) -> None:
    if config.operator.provider is not OperatorProviderId.TELEGRAM:
        return
    token = secrets.telegram.bot_token
    if token is None:
        problems.append(_problem(P.OPERATOR_CHANNEL, C.MISSING_SECRET, "TELEGRAM_BOT_TOKEN"))
    elif not TELEGRAM_TOKEN.fullmatch(token.get_secret_value()):  # shape only; the value is never reported
        problems.append(_problem(P.OPERATOR_CHANNEL, C.INVALID_PROVIDER_CONFIG, "TELEGRAM_BOT_TOKEN"))
    operators = config.operator.operators
    chats = [o.chat_id for o in operators]
    names = [o.operator_id for o in operators]
    if not operators:
        problems.append(_problem(P.OPERATOR_CHANNEL, C.MISSING_SETTING, "TELEGRAM_OPERATOR_CHAT_IDS"))
    elif (len(set(chats)) != len(chats) or len(set(names)) != len(names)
          or (operator_ids and not set(names) <= set(operator_ids))):
        # One private chat per operator, one operator per chat, and only Stage 7 operators.
        problems.append(_problem(P.OPERATOR_CHANNEL, C.INVALID_PROVIDER_CONFIG, "TELEGRAM_OPERATOR_CHAT_IDS"))


def _knowledge(config: IntegrationConfig, problems: list[IntegrationProblem]) -> None:
    directory = config.knowledge.directory
    if directory is not None and not directory.is_dir():
        problems.append(_problem(P.KNOWLEDGE, C.INVALID_PROVIDER_CONFIG, "KNOWLEDGE_DIR"))


def _inside_code_tree(path: Path) -> bool:
    resolved = path.resolve()
    return resolved.is_relative_to(REPO_ROOT) and not resolved.is_relative_to(LOCAL_DIR)


def _credential_file(path: Path, variable: str, problems: list[IntegrationProblem],
                     warnings: list[IntegrationProblem]) -> None:
    """Metadata only: the file is never opened."""
    if _inside_code_tree(path):
        problems.append(_problem(P.EMAIL, C.SECRET_SOURCE_INVALID, variable))  # could be committed by accident
    if not path.is_file():
        problems.append(_problem(P.EMAIL, C.CREDENTIAL_FILE_MISSING, variable))
        return
    if not os.access(path, os.R_OK):
        problems.append(_problem(P.EMAIL, C.CREDENTIAL_FILE_UNREADABLE, variable))
    if os.name == "posix" and stat.S_IMODE(path.stat().st_mode) & 0o077:
        warnings.append(_problem(P.EMAIL, C.CREDENTIAL_FILE_PERMISSIONS_BROAD, variable))


def _token_file(path: Path, problems: list[IntegrationProblem]) -> None:
    """The token file may not exist yet (a future OAuth flow writes it); its directory must."""
    if _inside_code_tree(path):
        problems.append(_problem(P.EMAIL, C.SECRET_SOURCE_INVALID, "GMAIL_TOKEN_FILE"))
    if path.exists() and not path.is_file():
        problems.append(_problem(P.EMAIL, C.INVALID_PROVIDER_CONFIG, "GMAIL_TOKEN_FILE"))
    elif not path.parent.is_dir():
        problems.append(_problem(P.EMAIL, C.CREDENTIAL_FILE_MISSING, "GMAIL_TOKEN_FILE"))
