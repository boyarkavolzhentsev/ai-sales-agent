"""Provider secrets, kept apart from ordinary configuration.

Every value is a ``SecretStr``: its repr, str and JSON serialization are masked, so a
secret cannot leak through a log line, a validation error, a health report or the CLI by
accident. Only a future provider adapter calls ``get_secret_value()``. Sources are the
``SALES_AGENT_*`` environment (and, for Gmail, a local credential file whose path is
configuration and whose contents are never read here)."""

from pydantic import SecretStr

from app.core.models.base import CoreModel


class GmailSecrets(CoreModel):
    client_id: SecretStr | None = None
    client_secret: SecretStr | None = None
    refresh_token: SecretStr | None = None


class LLMSecrets(CoreModel):
    api_key: SecretStr | None = None


class TelegramSecrets(CoreModel):
    bot_token: SecretStr | None = None


class ProviderSecrets(CoreModel):
    gmail: GmailSecrets = GmailSecrets()
    llm: LLMSecrets = LLMSecrets()
    telegram: TelegramSecrets = TelegramSecrets()
