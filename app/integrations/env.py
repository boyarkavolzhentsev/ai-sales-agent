"""Integration variables of the existing ``SALES_AGENT_*`` environment loader.

``app.runtime.env`` owns the namespace (unknown variables are rejected there); this module
declares the integration variable names and turns their values into the non-secret
``IntegrationConfig`` and the ``ProviderSecrets``. Values are never echoed: a problem names
the variable and a code. Empty values count as unset. Only explicitly given fields are set
on the models, so a setting for an unselected provider is detected, not ignored.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field

from pydantic import SecretStr, ValidationError

from app.core.models.base import CoreModel
from app.integrations.config import (
    EmailProviderConfig,
    EmbeddingsProviderConfig,
    IntegrationConfig,
    KnowledgeProviderConfig,
    LLMProviderConfig,
    OperatorChannelConfig,
)
from app.integrations.errors import IntegrationCode, IntegrationProblem
from app.integrations.providers import ProviderCategory
from app.integrations.secrets import GmailSecrets, LLMSecrets, ProviderSecrets, TelegramSecrets
from app.integrations.status import CATEGORY_OF_SECTION, SECRET_VARIABLES, VARIABLES

SETTING_VARIABLES = frozenset(VARIABLES.values())
SECRET_NAMES = frozenset(SECRET_VARIABLES.values())
INTEGRATION_VARIABLES = SETTING_VARIABLES | SECRET_NAMES
_INTEGERS = frozenset({"GMAIL_POLL_INTERVAL_SECONDS", "GMAIL_TIMEOUT_SECONDS", "LLM_TIMEOUT_SECONDS"})
_SECTIONS: dict[str, type[CoreModel]] = {"email": EmailProviderConfig, "llm": LLMProviderConfig,
                                          "operator": OperatorChannelConfig, "knowledge": KnowledgeProviderConfig,
                                          "embeddings": EmbeddingsProviderConfig}


@dataclass(frozen=True)
class ParsedIntegrations:
    config: IntegrationConfig
    secrets: ProviderSecrets
    problems: tuple[IntegrationProblem, ...] = field(default=())


def parse_integrations(values: Mapping[str, str]) -> ParsedIntegrations:
    """``values``: the environment with the ``SALES_AGENT_`` prefix removed. A section
    that cannot be parsed falls back to its defaults and its problems are returned."""
    problems: list[IntegrationProblem] = []
    sections: dict[str, CoreModel] = {}
    for section, model in _SECTIONS.items():
        raw: dict[str, object] = {}
        for (owner, name), variable in VARIABLES.items():
            text = values.get(variable, "").strip()
            if owner != section or not text:
                continue
            value = _value(variable, text)
            if value is None:
                problems.append(_problem(section, IntegrationCode.INVALID_PROVIDER_CONFIG, variable))
                continue
            raw[name] = value
        try:
            sections[section] = model.model_validate(raw)
        except ValidationError as exc:
            bad = {str(error["loc"][0]) if error["loc"] else "provider" for error in exc.errors()}
            for name in sorted(bad):  # location only: pydantic's own text may include the value
                variable = VARIABLES.get((section, name), VARIABLES[(section, "provider")])
                code = IntegrationCode.UNKNOWN_PROVIDER if name == "provider" else IntegrationCode.INVALID_PROVIDER_CONFIG
                problems.append(_problem(section, code, variable))
            # Keep the valid fields (and the selected provider) so one bad value does not
            # turn into misleading follow-on problems; an unknown provider falls back to defaults.
            kept = {name: value for name, value in raw.items() if name not in bad}
            sections[section] = model() if "provider" in bad else model.model_validate(kept)
    config = IntegrationConfig.model_validate(sections)
    secret = {variable: SecretStr(values[variable].strip()) for variable in SECRET_NAMES if values.get(variable, "").strip()}
    secrets = ProviderSecrets(
        gmail=GmailSecrets(client_id=secret.get("GMAIL_CLIENT_ID"), client_secret=secret.get("GMAIL_CLIENT_SECRET"),
                           refresh_token=secret.get("GMAIL_REFRESH_TOKEN")),
        llm=LLMSecrets(api_key=secret.get("LLM_API_KEY")),
        telegram=TelegramSecrets(bot_token=secret.get("TELEGRAM_BOT_TOKEN")),
    )
    return ParsedIntegrations(config=config, secrets=secrets, problems=tuple(problems))


def _value(variable: str, text: str) -> object | None:
    """The typed raw value, or None when malformed (reported without the value)."""
    if variable.endswith("_PROVIDER"):
        return text.upper()
    if variable in _INTEGERS:
        return int(text) if text.isdigit() else None
    if variable == "TELEGRAM_OPERATOR_CHAT_IDS":
        items = [item.strip() for item in text.split(",") if item.strip()]
        if not items or not all(item.lstrip("-").isdigit() for item in items):
            return None
        return tuple(int(item) for item in items)
    return text


def _problem(section: str, code: IntegrationCode, variable: str) -> IntegrationProblem:
    category: ProviderCategory = CATEGORY_OF_SECTION[section]
    return IntegrationProblem(category=category, code=code, variable=variable)
