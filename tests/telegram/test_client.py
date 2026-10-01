"""The Bot API client over a fake HTTP session (no network): exactly the five methods,
plain text, bounded timeouts, no retries, error mapping by code only, the token never in
an error or a log line; strict callback data; rendering of untrusted text."""

import logging
from dataclasses import dataclass, field
from typing import Any

import pytest
import requests
from pydantic import SecretStr

from app.integrations.telegram.callbacks import MAX_BYTES, Action, decode, encode
from app.integrations.telegram.client import TelegramClient, parse_update
from app.integrations.telegram.errors import TelegramCode, TelegramError
from app.integrations.telegram.rendering import MAX_MESSAGE, TRUNCATED, clean, excerpt, fit
from tests.telegram.builders import BOT_TOKEN

ID = "ob_" + "a" * 40


@dataclass
class Response:
    status_code: int
    body: Any

    def json(self) -> Any:
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


@dataclass
class Session:
    responses: list[Any] = field(default_factory=list)
    posts: list[tuple[str, dict[str, Any], float]] = field(default_factory=list)

    def post(self, url: str, *, json: dict[str, Any], timeout: float) -> Response:
        self.posts.append((url, json, timeout))
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def ok(result: Any) -> Response:
    return Response(200, {"ok": True, "result": result})


def client(*responses: Any) -> tuple[TelegramClient, Session]:
    session = Session(list(responses))
    return TelegramClient(SecretStr(BOT_TOKEN), timeout_seconds=7, session=session), session


def test_requests_are_the_five_methods_plain_text_and_bounded() -> None:
    api, session = client(ok({"id": 42, "username": "bot"}), ok([]), ok({"message_id": 9}), ok(True), ok(True))
    assert (api.get_me().bot_id, api.get_updates(offset=5, limit=10, timeout=0), api.send_message(1, "hi <b>x</b>",
            buttons=((("Approve", "a|x|1"),),)).message_id) == (42, (), 9)
    api.edit_message_text(1, 9, "closed")
    api.answer_callback_query("cb", "x" * 500)
    methods = [url.rsplit("/", 1)[1] for url, _, _ in session.posts]
    assert methods == ["getMe", "getUpdates", "sendMessage", "editMessageText", "answerCallbackQuery"]
    assert all(url.startswith("https://api.telegram.org/bot") for url, _, _ in session.posts)
    assert all(timeout == 7 for _, _, timeout in session.posts)  # short poll: timeout 0 adds nothing
    _, updates, _ = session.posts[1]
    assert updates == {"limit": 10, "timeout": 0, "allowed_updates": ["message", "callback_query"], "offset": 5}
    _, send, _ = session.posts[2]
    assert "parse_mode" not in send and send["text"] == "hi <b>x</b>" and send["disable_web_page_preview"] is True
    assert send["reply_markup"] == {"inline_keyboard": [[{"text": "Approve", "callback_data": "a|x|1"}]]}
    assert len(session.posts[4][1]["text"]) == 200
    assert repr(api) == "TelegramClient(<redacted>)" and BOT_TOKEN not in repr(api)


@pytest.mark.parametrize(("response", "code"), [
    (Response(401, {"ok": False, "description": "Unauthorized"}), TelegramCode.AUTH_INVALID),
    (Response(403, {"ok": False, "description": "bot was blocked by the user"}), TelegramCode.FORBIDDEN),
    (Response(429, {"ok": False, "parameters": {"retry_after": 5}}), TelegramCode.RATE_LIMITED),
    (Response(502, ValueError("not json")), TelegramCode.TEMPORARY_PROVIDER_ERROR),
    (Response(500, {"ok": False}), TelegramCode.TEMPORARY_PROVIDER_ERROR),
    (Response(400, {"ok": False, "description": "Bad Request: query is too old"}), TelegramCode.CALLBACK_EXPIRED),
    (Response(400, {"ok": False, "description": "Bad Request: message is not modified"}), TelegramCode.MESSAGE_NOT_MODIFIED),
    (Response(400, {"ok": False, "description": "Bad Request: message to edit not found"}), TelegramCode.MESSAGE_NOT_FOUND),
    (Response(400, {"ok": False, "description": f"chat {BOT_TOKEN} weird"}), TelegramCode.BAD_REQUEST),
    (Response(200, ["not", "an", "object"]), TelegramCode.UNEXPECTED_RESPONSE),
    (requests.ConnectionError(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"), TelegramCode.NETWORK_ERROR),
    (requests.Timeout(f"/bot{BOT_TOKEN}/sendMessage timed out"), TelegramCode.NETWORK_ERROR),
])
def test_errors_are_codes_without_content_and_nothing_retries(response: Any, code: TelegramCode) -> None:
    api, session = client(response)
    with pytest.raises(TelegramError) as error:
        api.send_message(1, "hi")
    assert error.value.code is code and len(session.posts) == 1  # one request, no hidden retry
    assert BOT_TOKEN not in str(error.value) and BOT_TOKEN not in repr(error.value)
    assert error.value.__cause__ is None and (error.value.__context__ is None or error.value.__suppress_context__)
    if code is TelegramCode.RATE_LIMITED:
        assert error.value.retry_after == 5


@pytest.mark.parametrize("result", [{"username": "no id"}, {"id": "42"}, {"id": True}, None])
def test_a_malformed_get_me_is_a_provider_error(result: Any) -> None:
    api, _ = client(ok(result))
    with pytest.raises(TelegramError) as error:
        api.get_me()
    assert error.value.code is TelegramCode.UNEXPECTED_RESPONSE


@pytest.mark.parametrize("result", [[{"update_id": "7"}], [{"no": "id"}], ["x"], {"update_id": 1}])
def test_a_malformed_update_batch_is_refused_whole(result: Any) -> None:
    api, _ = client(ok(result))
    with pytest.raises(TelegramError) as error:
        api.get_updates(offset=None, limit=5, timeout=0)
    assert error.value.code is TelegramCode.UNEXPECTED_RESPONSE


def test_updates_keep_only_what_the_console_needs() -> None:
    message = parse_update({"update_id": 1, "message": {"message_id": 3, "from": {"id": 5, "first_name": "Eve", "is_bot": False},
                                                        "chat": {"id": 5, "type": "private", "title": "x"}, "text": "/queue"}})
    assert (message.kind, message.user_id, message.chat_id, message.chat_type, message.text) == ("message", 5, 5, "private", "/queue")
    callback = parse_update({"update_id": 2, "callback_query": {"id": "99", "from": {"id": 5}, "data": "a|x|1",
                                                                "message": {"message_id": 8, "chat": {"id": 5, "type": "private"}}}})
    assert (callback.kind, callback.callback_id, callback.callback_data, callback.message_id) == ("callback_query", "99", "a|x|1", 8)
    for other in ({"update_id": 3, "edited_message": {}}, {"update_id": 4, "channel_post": {"text": "x"}},
                  {"update_id": 5, "message": "not an object"}):
        assert parse_update(other).kind == "other"
    spoofed = parse_update({"update_id": 6, "message": {"from": {"id": True}, "chat": {"id": "5", "type": "private"}}})
    assert (spoofed.user_id, spoofed.chat_id, spoofed.text) == (None, None, None)


def test_debug_log_lines_never_carry_the_token(caplog: pytest.LogCaptureFixture) -> None:
    client()  # installs the redaction filter
    logger = logging.getLogger("urllib3.connectionpool")
    with caplog.at_level(logging.DEBUG, logger="urllib3.connectionpool"):
        logger.debug('%s://%s:%s "%s %s %s" %s %s', "https", "api.telegram.org", 443, "POST",
                     f"/bot{BOT_TOKEN}/getUpdates", "HTTP/1.1", 200, None)
        logger.debug(f"Starting POST /bot{BOT_TOKEN}/sendMessage")
        logger.debug("mapping %(path)s", {"path": f"/bot{BOT_TOKEN}/getMe"})
    assert caplog.records and BOT_TOKEN not in caplog.text and "/bot<redacted>/" in caplog.text


# ---- Callback data --------------------------------------------------------------------------------------------


def test_callback_data_round_trips_within_64_bytes() -> None:
    for action, version, argument in ((Action.APPROVE_DRAFT, 3, None), (Action.REJECT_DRAFT_REASON, 3, 2),
                                      (Action.LOST_REASON, 999_999_999, 11)):
        data = encode(action, ID, version, argument)
        assert len(data.encode()) <= MAX_BYTES
        parsed = decode(data)
        assert parsed is not None and (parsed.action, parsed.target, parsed.version, parsed.argument) == (action, ID, version, argument)
    token = encode(Action.CONFIRM, "0123456789abcdef")
    assert decode(token).target == "0123456789abcdef"  # type: ignore[union-attr]
    with pytest.raises(ValueError):
        encode(Action.APPROVE_DRAFT, "x" * 70, 1)


@pytest.mark.parametrize("data", [
    None, "", "zz|x|1", "a", "a|x", "a|x|1|2", "a|x|-1", "a|x|1.0", "a|x|abc", "a|../x|1", "a|x y|1", "a|x|1234567890",
    "rr|x|1", "lr|x|1", "cf|0123", "cf|0123456789ABCDEF", "cf|0123456789abcdef|1", "a|" + "x" * 64 + "|1",
    "a|x|１", "a|ｘ|1", "a|x|1\n", "w|" + ID + "|1|1",
])
def test_forged_or_malformed_callback_data_is_refused(data: str | None) -> None:
    assert decode(data) is None


# ---- Rendering --------------------------------------------------------------------------------------------------


def test_untrusted_text_is_cleaned_and_truncated_deterministically() -> None:
    hostile = "Pay now\u202e\u200b<a href='x'>link</a>\x00\x07 *bold* `code`\n\tok"
    assert clean(hostile) == "Pay now<a href='x'>link</a> *bold* `code`\n\tok"  # plain text; markup stays inert text
    long = "word " * 2000
    assert excerpt(long, 100).endswith(TRUNCATED) and len(excerpt(long, 100)) <= 100
    assert excerpt(long, 100) == excerpt(long, 100) and excerpt("short", 100) == "short"
    assert len(fit("x" * 10_000)) == MAX_MESSAGE and excerpt(None, 10) == ""


def _never_connected() -> requests.ConnectionError:
    from urllib3.exceptions import MaxRetryError, NewConnectionError
    return requests.ConnectionError(MaxRetryError(None, "/bot<redacted>/sendMessage",  # type: ignore[arg-type]
                                                  NewConnectionError(None, "Failed to establish a new connection")))  # type: ignore[arg-type]


@pytest.mark.parametrize(("response", "uncertain"), [
    (requests.ConnectTimeout("connect timed out"), False),  # never connected: nothing submitted
    (_never_connected(), False),  # DNS failure / refused
    (requests.ReadTimeout("read timed out"), True),  # submitted, the answer never came
    (requests.ConnectionError("Connection aborted: RemoteDisconnected"), True),  # dropped after sending
    (OSError("socket closed"), True),
    (Response(502, ValueError("not json")), True),  # a proxy's 5xx: may have been processed
    (Response(500, {"ok": False}), True),
    (Response(200, ValueError("truncated")), True),  # an unreadable success
    (ok({"no": "message_id"}), True),  # Telegram said ok: the message exists
    (Response(429, {"ok": False, "parameters": {"retry_after": 1}}), False),  # refused
    (Response(403, {"ok": False}), False),
    (Response(400, {"ok": False, "description": "Bad Request: chat not found"}), False),
])
def test_send_failures_say_whether_the_message_may_exist(response: Any, uncertain: bool) -> None:
    api, session = client(response)
    with pytest.raises(TelegramError) as error:
        api.send_message(1, "card")
    assert error.value.uncertain is uncertain and len(session.posts) == 1
