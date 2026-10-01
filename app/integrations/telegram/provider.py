"""Assembling the Telegram adapters: one ``getMe`` at startup proves the token works and
yields the bot identity (id and username only, kept in memory). The token is never
stored, printed or put in an error; rotating it needs no database change (the cursor is
keyed by the bot id)."""

from collections.abc import Callable
from dataclasses import dataclass

from pydantic import SecretStr

from app.integrations.config import OperatorChannelConfig
from app.integrations.secrets import TelegramSecrets
from app.integrations.telegram.auth import TelegramOperatorAuthenticator
from app.integrations.telegram.client import BotIdentity, TelegramApi, TelegramClient
from app.integrations.telegram.errors import TelegramCode, TelegramError

ApiFactory = Callable[[SecretStr, int], TelegramApi]


def real_api(token: SecretStr, timeout_seconds: int) -> TelegramApi:
    return TelegramClient(token, timeout_seconds=timeout_seconds)


@dataclass(frozen=True)
class TelegramAdapters:
    api: TelegramApi
    bot: BotIdentity
    authenticator: TelegramOperatorAuthenticator

    def __repr__(self) -> str:
        return f"TelegramAdapters(bot_id={self.bot.bot_id})"


def build_telegram(config: OperatorChannelConfig, secrets: TelegramSecrets, *,
                   api_factory: ApiFactory = real_api) -> TelegramAdapters:
    if secrets.bot_token is None:
        raise TelegramError(TelegramCode.AUTH_INVALID)
    api = api_factory(secrets.bot_token, config.timeout_seconds)
    bot = api.get_me()
    return TelegramAdapters(api=api, bot=bot, authenticator=TelegramOperatorAuthenticator(
        {operator.chat_id: operator.operator_id for operator in config.operators}))
