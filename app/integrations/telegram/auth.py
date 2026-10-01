"""Telegram identity -> Stage 7 credential.

A Telegram update fetched with our bot token is Telegram's statement of who acted. The
console turns it into an ``OperatorCredential`` (scheme "telegram", token
"<user id>:<chat id>"); ``TelegramOperatorAuthenticator`` maps it to an operator id only
for a configured private chat, where the chat id equals the user id. Display names,
usernames and message text are never used. Stage 7 then still checks the operator id
against its own allow-list, so this adds a way to authenticate, never a way around it.
"""

from collections.abc import Mapping

from pydantic import SecretStr

from app.operator.auth import OperatorAuthenticator
from app.operator.models import OperatorCredential

SCHEME = "telegram"


def credential_for(user_id: int, chat_id: int) -> OperatorCredential:
    return OperatorCredential(scheme=SCHEME, token=SecretStr(f"{user_id}:{chat_id}"))


class TelegramOperatorAuthenticator:
    def __init__(self, operators: Mapping[int, str]) -> None:
        self._operators = dict(operators)

    def configured(self) -> dict[int, str]:
        """Private chat id -> operator id, as configured."""
        return dict(self._operators)

    def operator_for(self, user_id: int | None, chat_id: int | None, chat_type: str | None) -> str | None:
        """Private chats only, and only the configured user in their own chat."""
        if user_id is None or chat_id is None or chat_type != "private" or user_id != chat_id:
            return None
        return self._operators.get(user_id)

    def authenticate(self, credential: OperatorCredential) -> str | None:
        if credential.scheme != SCHEME:
            return None
        user, _, chat = credential.token.get_secret_value().partition(":")
        if not (user.isdigit() and chat.isdigit()):
            return None
        return self.operator_for(int(user), int(chat), "private")


class SchemeAuthenticator:
    """Routes a credential to the authenticator of its scheme: Telegram credentials to
    Telegram, everything else to the previously configured authenticator (e.g. an injected
    one in tests; DenyAll in production)."""

    def __init__(self, telegram: TelegramOperatorAuthenticator, fallback: OperatorAuthenticator) -> None:
        self._telegram = telegram
        self._fallback = fallback

    def authenticate(self, credential: OperatorCredential) -> str | None:
        if credential.scheme == SCHEME:
            return self._telegram.authenticate(credential)
        return self._fallback.authenticate(credential)
