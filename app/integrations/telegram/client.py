"""A narrow Telegram Bot API client (direct HTTPS; no bot framework, no event loop).

Only getMe, getUpdates, sendMessage, editMessageText and answerCallbackQuery. Every call
is one request with a bounded timeout: nothing retries behind the caller, so a message or
an acknowledgement is never sent twice by this layer. Responses become small DTOs; an
unknown update type becomes an ``Update`` of kind "other" (never a crash).

The token is part of every Bot API URL. It is held as a SecretStr, never placed in an
error, and a logging filter redacts it from urllib3's debug lines; request exceptions are
mapped to codes without their message (which can contain the URL).
"""

import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import SecretStr
from requests import exceptions as http
from urllib3 import exceptions as wire

from app.integrations.telegram.errors import TelegramCode, TelegramError

API = "https://api.telegram.org"
ALLOWED_UPDATES = ("message", "callback_query")
_TOKEN_IN_PATH = re.compile(r"/bot\d+:[A-Za-z0-9_-]+")


class _RedactBotToken(logging.Filter):
    """urllib3 logs request lines (``POST /bot<token>/sendMessage``) at DEBUG."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(_redact(a) for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: _redact(v) for k, v in record.args.items()}
        if isinstance(record.msg, str):
            record.msg = _TOKEN_IN_PATH.sub("/bot<redacted>", record.msg)
        return True


def _redact(value: object) -> object:
    return _TOKEN_IN_PATH.sub("/bot<redacted>", value) if isinstance(value, str) else value


_FILTER = _RedactBotToken()


def install_log_redaction() -> None:
    for name in ("urllib3.connectionpool", "urllib3", "requests"):
        logger = logging.getLogger(name)
        if _FILTER not in logger.filters:
            logger.addFilter(_FILTER)


@dataclass(frozen=True)
class BotIdentity:
    bot_id: int
    username: str | None


@dataclass(frozen=True)
class Update:
    """The fields the console needs; nothing else of the raw update is kept."""

    update_id: int
    kind: str  # "message", "callback_query" or "other"
    user_id: int | None = None
    chat_id: int | None = None
    chat_type: str | None = None
    text: str | None = None
    callback_id: str | None = None
    callback_data: str | None = None
    message_id: int | None = None  # the message a callback button belongs to


@dataclass(frozen=True)
class SentMessage:
    message_id: int


class TelegramApi(Protocol):
    def get_me(self) -> BotIdentity: ...
    def get_updates(self, *, offset: int | None, limit: int, timeout: int) -> tuple[Update, ...]: ...
    def send_message(self, chat_id: int, text: str, *, buttons: tuple[tuple[tuple[str, str], ...], ...] = ()) -> SentMessage: ...
    def edit_message_text(self, chat_id: int, message_id: int, text: str) -> None: ...
    def answer_callback_query(self, callback_id: str, text: str) -> None: ...


def parse_update(raw: dict[str, Any]) -> Update:
    update_id = _required_int(raw["update_id"])
    if isinstance(raw.get("message"), dict):
        message = raw["message"]
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        text = message.get("text")
        return Update(update_id=update_id, kind="message", user_id=_int(sender.get("id")), chat_id=_int(chat.get("id")),
                      chat_type=chat.get("type"), text=text if isinstance(text, str) else None)
    if isinstance(raw.get("callback_query"), dict):
        query = raw["callback_query"]
        message = query.get("message") or {}
        chat = message.get("chat") or {}
        sender = query.get("from") or {}
        data = query.get("data")
        return Update(update_id=update_id, kind="callback_query", user_id=_int(sender.get("id")), chat_id=_int(chat.get("id")),
                      chat_type=chat.get("type"), callback_id=str(query.get("id")) if query.get("id") else None,
                      callback_data=data if isinstance(data, str) else None, message_id=_int(message.get("message_id")))
    return Update(update_id=update_id, kind="other")


def _required_int(value: object) -> int:
    if _int(value) is None:
        raise ValueError("not an integer")
    return value  # type: ignore[return-value]


@contextmanager
def _shape(*, uncertain: bool = False) -> Iterator[None]:
    """A response of an unexpected shape is a provider error, never a crash (no content)."""
    try:
        yield
    except (KeyError, TypeError, ValueError, AttributeError):
        raise TelegramError(TelegramCode.UNEXPECTED_RESPONSE, uncertain=uncertain) from None


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class TelegramClient:
    def __init__(self, token: SecretStr, *, timeout_seconds: int, session: Any = None) -> None:
        self._token = token
        self._timeout = timeout_seconds
        self._session = session
        install_log_redaction()

    def __repr__(self) -> str:
        return "TelegramClient(<redacted>)"

    def get_me(self) -> BotIdentity:
        data = self._call("getMe", {})
        with _shape():
            return BotIdentity(bot_id=_required_int(data["id"]), username=data.get("username"))

    def get_updates(self, *, offset: int | None, limit: int, timeout: int) -> tuple[Update, ...]:
        body: dict[str, Any] = {"limit": limit, "timeout": timeout, "allowed_updates": list(ALLOWED_UPDATES)}
        if offset is not None:
            body["offset"] = offset
        result = self._call("getUpdates", body, extra_timeout=timeout)
        if not isinstance(result, list):
            raise TelegramError(TelegramCode.UNEXPECTED_RESPONSE)
        with _shape():  # an update we cannot even number cannot be confirmed: refuse the batch
            return tuple(parse_update(item) for item in result)

    def send_message(self, chat_id: int, text: str, *, buttons: tuple[tuple[tuple[str, str], ...], ...] = ()) -> SentMessage:
        body: dict[str, Any] = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if buttons:
            body["reply_markup"] = {"inline_keyboard": [[{"text": label, "callback_data": data} for label, data in row]
                                                        for row in buttons]}
        data = self._call("sendMessage", body)  # plain text: no parse_mode, so nothing in it is formatting
        with _shape(uncertain=True):  # Telegram said ok: the message most likely exists
            return SentMessage(message_id=_required_int(data["message_id"]))

    def edit_message_text(self, chat_id: int, message_id: int, text: str) -> None:
        self._call("editMessageText", {"chat_id": chat_id, "message_id": message_id, "text": text,
                                       "disable_web_page_preview": True})

    def answer_callback_query(self, callback_id: str, text: str) -> None:
        self._call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:200]})

    # ---- Transport ------------------------------------------------------------------------------

    def _http(self) -> Any:
        if self._session is None:
            import requests

            self._session = requests.Session()  # requests performs no retries by default (its adapters use 0)
        return self._session

    def _call(self, method: str, body: dict[str, Any], *, extra_timeout: int = 0) -> Any:
        url = f"{API}/bot{self._token.get_secret_value()}/{method}"
        try:
            response = self._http().post(url, json=body, timeout=self._timeout + extra_timeout)
        except (http.RequestException, OSError) as exc:
            # Never the exception's message: it may hold the URL (and so the token).
            raise TelegramError(TelegramCode.NETWORK_ERROR, uncertain=not _never_connected(exc)) from None
        try:
            payload = response.json()
        except ValueError:
            payload = None
        status = int(response.status_code)
        if not isinstance(payload, dict):
            code = TelegramCode.TEMPORARY_PROVIDER_ERROR if status >= 500 else TelegramCode.UNEXPECTED_RESPONSE
            # A 5xx (often a proxy) or an unreadable 2xx: the request may have been processed.
            raise TelegramError(code, status=status, uncertain=status >= 500 or status < 400)
        if payload.get("ok") is True and status < 400:
            return payload.get("result")
        raise _error(status, payload)


def _never_connected(exc: BaseException) -> bool:
    """True only when no connection was established, so nothing was submitted: a connect
    timeout, or a connection error caused by failing to connect (DNS, refused). A read
    timeout or a dropped connection may follow a submitted request."""
    if isinstance(exc, http.ConnectTimeout):
        return True
    if isinstance(exc, http.ConnectionError) and not isinstance(exc, http.ReadTimeout):
        reason = exc.args[0] if exc.args else None
        reason = getattr(reason, "reason", reason)
        return isinstance(reason, wire.NewConnectionError)
    return False


def _error(status: int, payload: dict[str, Any]) -> TelegramError:
    description = str(payload.get("description", "")).lower()  # used to classify only; never surfaced
    retry_after = (payload.get("parameters") or {}).get("retry_after")
    if status == 401:
        return TelegramError(TelegramCode.AUTH_INVALID, status=status)
    if status == 403:
        return TelegramError(TelegramCode.FORBIDDEN, status=status)
    if status == 429:
        return TelegramError(TelegramCode.RATE_LIMITED, status=status, retry_after=retry_after if isinstance(retry_after, int) else None)
    if status >= 500:  # may have been processed before the error: uncertain
        return TelegramError(TelegramCode.TEMPORARY_PROVIDER_ERROR, status=status, uncertain=True)
    if "query is too old" in description or "query id is invalid" in description:
        return TelegramError(TelegramCode.CALLBACK_EXPIRED, status=status)
    if "message is not modified" in description:
        return TelegramError(TelegramCode.MESSAGE_NOT_MODIFIED, status=status)
    if "message to edit not found" in description or "message not found" in description:
        return TelegramError(TelegramCode.MESSAGE_NOT_FOUND, status=status)
    return TelegramError(TelegramCode.BAD_REQUEST, status=status)
