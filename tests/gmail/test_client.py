"""The narrow Gmail REST client: one request per call (never retried), bounded timeouts,
HTTP outcomes mapped to stable codes and delivery knowledge, no provider text in errors."""

import base64
from typing import Any

import pytest
from google.oauth2.credentials import Credentials
from requests import exceptions as http

from app.integrations.gmail.client import BASE_URL, USER_AGENT, GmailClient
from app.integrations.gmail.errors import Delivery, GmailCode, GmailError
from tests.gmail.builders import ACCESS_TOKEN


class Response:
    def __init__(self, status: int, payload: object = None, text: str | None = None) -> None:
        self.status_code = status
        self._payload = payload
        self._text = text

    def json(self) -> object:
        if self._text is not None:
            raise ValueError("not json")
        return self._payload


class Session:
    def __init__(self, *outcomes: object) -> None:
        self.outcomes = list(outcomes)
        self.requests: list[dict[str, Any]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> Response:
        self.requests.append({"method": method, "url": url, **kwargs})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome  # type: ignore[return-value]


class Auth:
    def __init__(self) -> None:
        self.calls = 0

    def credentials(self) -> Credentials:
        self.calls += 1
        return Credentials(ACCESS_TOKEN)


def client(*outcomes: object) -> tuple[GmailClient, Session, Auth]:
    session, auth = Session(*outcomes), Auth()
    return GmailClient(auth, timeout_seconds=7, session=session), session, auth  # type: ignore[arg-type]


def test_send_posts_base64url_raw_once_with_a_timeout() -> None:
    gmail, session, auth = client(Response(200, {"id": "s1", "threadId": "t1", "labelIds": ["SENT"]}))
    sent = gmail.send(b"Subject: x\r\n\r\nbody \xc3\xa9", thread_id="t1")
    assert (sent.message_id, sent.thread_id) == ("s1", "t1")
    [request] = session.requests
    assert (request["method"], request["url"], request["timeout"]) == ("POST", f"{BASE_URL}/messages/send", 7)
    assert base64.urlsafe_b64decode(request["json"]["raw"]) == b"Subject: x\r\n\r\nbody \xc3\xa9"
    assert request["json"]["threadId"] == "t1" and auth.calls == 1  # credentials are made fresh first


@pytest.mark.parametrize(("outcome", "code", "delivery"), [
    (Response(400, {"error": {"message": "secret detail"}}), GmailCode.INVALID_REQUEST, Delivery.REJECTED),
    (Response(401, {}), GmailCode.AUTH_REQUIRED, Delivery.REJECTED),
    (Response(403, {"error": {"errors": [{"reason": "insufficientPermissions"}]}}), GmailCode.PERMISSION_DENIED, Delivery.REJECTED),
    (Response(403, {"error": {"errors": [{"reason": "userRateLimitExceeded"}]}}), GmailCode.RATE_LIMITED, Delivery.REJECTED),
    (Response(429, {}), GmailCode.RATE_LIMITED, Delivery.REJECTED),
    (Response(500, {}), GmailCode.TEMPORARY_PROVIDER_ERROR, Delivery.UNKNOWN),
    (Response(503, {}), GmailCode.TEMPORARY_PROVIDER_ERROR, Delivery.UNKNOWN),
    (Response(200, text="<html>proxy</html>"), GmailCode.UNEXPECTED_RESPONSE, Delivery.UNKNOWN),
    (Response(200, {"threadId": "t1"}), GmailCode.UNEXPECTED_RESPONSE, Delivery.UNKNOWN),  # no id: unprovable
    (http.ConnectTimeout(), GmailCode.TEMPORARY_PROVIDER_ERROR, Delivery.NOT_SENT),
    (http.ReadTimeout(), GmailCode.TEMPORARY_PROVIDER_ERROR, Delivery.UNKNOWN),
    (http.ConnectionError(), GmailCode.TEMPORARY_PROVIDER_ERROR, Delivery.UNKNOWN),
    (ConnectionResetError(), GmailCode.TEMPORARY_PROVIDER_ERROR, Delivery.UNKNOWN),
])
def test_send_outcomes_map_conservatively_and_are_never_retried(outcome: object, code: GmailCode, delivery: Delivery) -> None:
    gmail, session, _ = client(outcome, Response(200, {"id": "s2"}))  # a second answer exists, but must not be used
    with pytest.raises(GmailError) as error:
        gmail.send(b"raw", thread_id=None)
    assert (error.value.code, error.value.delivery) == (code, delivery)
    assert len(session.requests) == 1  # exactly one request, whatever happened
    assert "secret detail" not in str(error.value) and error.value.__cause__ is None


def test_an_expired_history_start_is_reported_as_such() -> None:
    gmail, _, _ = client(Response(404, {}))
    with pytest.raises(GmailError) as error:
        gmail.history("12", page_token=None, max_results=10)
    assert error.value.code is GmailCode.HISTORY_EXPIRED


def test_reads_parse_into_provider_dtos() -> None:
    raw = base64.urlsafe_b64encode(b"From: a@b.example\r\n\r\nhi").decode().rstrip("=")
    gmail, session, _ = client(
        Response(200, {"emailAddress": "Sales@OurCo.example", "historyId": "77"}),
        Response(200, {"messages": [{"id": "m1"}, {"id": "m2"}]}),
        Response(200, {"id": "m1", "threadId": "t1", "labelIds": ["SENT"],
                       "payload": {"headers": [{"name": "Message-ID", "value": "<x@y>"}]}}),
        Response(200, {"id": "m1", "threadId": "t1", "labelIds": ["INBOX"], "internalDate": "1780315500000", "raw": raw}),
        Response(200, {"history": [{"id": "80", "messagesAdded": [{"message": {"id": "m3", "labelIds": ["INBOX"]}}]}],
                       "historyId": "81"}),
    )
    assert gmail.profile().email_address == "sales@ourco.example"  # normalized
    assert gmail.find_message_ids("rfc822msgid:x@y", max_results=5, include_spam_trash=True) == ("m1", "m2")
    assert gmail.get_metadata("m1", ("Message-ID",)).headers == {"message-id": "<x@y>"}
    assert gmail.get_raw("m1").raw == b"From: a@b.example\r\n\r\nhi"
    page = gmail.history("79", page_token=None, max_results=50)
    assert (page.records[0].history_id, page.records[0].added, page.history_id) == ("80", (("m3", ("INBOX",)),), "81")
    assert all(r["timeout"] == 7 for r in session.requests)
    assert session.requests[1]["params"]["includeSpamTrash"] == "true"
    assert session.requests[4]["params"] == {"startHistoryId": "79", "historyTypes": "messageAdded", "maxResults": 50}


def test_the_real_session_never_retries_or_refreshes_behind_the_callers_back() -> None:
    gmail = GmailClient(Auth(), timeout_seconds=5)  # type: ignore[arg-type]
    session: Any = gmail._session_for_call()  # noqa: SLF001
    assert session._refresh_status_codes == () and session._max_refresh_attempts == 0  # noqa: SLF001
    assert session.headers["User-Agent"] == USER_AGENT
    assert all(adapter.max_retries.total == 0 for adapter in session.adapters.values())
    assert "Bearer" not in repr(gmail) and ACCESS_TOKEN not in repr(gmail)


def test_one_session_is_reused_for_every_call() -> None:
    """Regression: a new HTTP session (connection pool) per call leaked connections."""
    gmail = GmailClient(Auth(), timeout_seconds=5)  # type: ignore[arg-type]
    assert gmail._session_for_call() is gmail._session_for_call()  # noqa: SLF001
