"""Stable Telegram provider error codes: never a token, URL or Telegram's own text."""

from enum import StrEnum


class TelegramCode(StrEnum):
    AUTH_INVALID = "AUTH_INVALID"  # the bot token was refused (401)
    FORBIDDEN = "FORBIDDEN"  # e.g. the operator blocked the bot (403)
    RATE_LIMITED = "RATE_LIMITED"  # 429 (``retry_after`` seconds when Telegram says so)
    TEMPORARY_PROVIDER_ERROR = "TEMPORARY_PROVIDER_ERROR"  # 5xx
    BAD_REQUEST = "BAD_REQUEST"
    MESSAGE_NOT_FOUND = "MESSAGE_NOT_FOUND"
    MESSAGE_NOT_MODIFIED = "MESSAGE_NOT_MODIFIED"
    CALLBACK_EXPIRED = "CALLBACK_EXPIRED"  # the callback query is too old to answer
    NETWORK_ERROR = "NETWORK_ERROR"
    UNEXPECTED_RESPONSE = "UNEXPECTED_RESPONSE"


class TelegramError(Exception):
    """``uncertain``: the request may have reached Telegram (a lost response, a read
    timeout, a 5xx, an unreadable success). Telegram has no idempotency key, so a send that
    failed this way must not be repeated automatically. False only when Telegram answered
    with a refusal or the connection was never established."""

    def __init__(self, code: TelegramCode, *, status: int | None = None, retry_after: int | None = None,
                 uncertain: bool = False) -> None:
        self.code = code
        self.status = status
        self.retry_after = retry_after
        self.uncertain = uncertain
        super().__init__(code.value if status is None else f"{code.value} (HTTP {status})")
