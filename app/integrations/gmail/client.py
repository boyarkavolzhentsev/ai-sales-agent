"""A narrow Gmail REST client: only the operations the adapters need, returning small
provider DTOs (business code never sees a Google object).

Transport: google-auth's ``AuthorizedSession`` (its official HTTP transport). It is built
with automatic refresh-and-retry disabled and the underlying HTTP layer performs no
retries, so every call is sent at most once: ``users.messages.send`` is never repeated
behind the caller's back. Credentials are refreshed (and persisted) explicitly before each
call; a refresh failure means nothing was sent. Every call has a bounded timeout.

Error mapping (codes only; never response bodies, tokens or URLs with secrets):
  HTTP 400 INVALID_REQUEST, 401 AUTH_REQUIRED, 403 PERMISSION_DENIED (or RATE_LIMITED for
  Google's rate-limit reasons), 404 NOT_FOUND, 429 RATE_LIMITED: Gmail answered, the
  operation was not performed (Delivery.REJECTED). 5xx TEMPORARY_PROVIDER_ERROR and any
  failure after the request may have reached Google (read timeout, reset connection,
  unparseable answer): Delivery.UNKNOWN. A connect timeout (nothing reached Google):
  Delivery.NOT_SENT.
"""

import base64
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from requests import exceptions as http  # the exception types of google-auth's HTTP transport

from app.integrations.gmail.auth import GmailAuth
from app.integrations.gmail.errors import Delivery, GmailCode, GmailError

BASE_URL = "https://gmail.googleapis.com/gmail/v1/users/me"
USER_AGENT = "ai-sales-agent-gmail/1"
RATE_LIMIT_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded", "dailyLimitExceeded"})


@dataclass(frozen=True)
class Profile:
    email_address: str
    history_id: str


@dataclass(frozen=True)
class SentMessage:
    message_id: str
    thread_id: str | None


@dataclass(frozen=True)
class MessageMetadata:
    message_id: str
    thread_id: str | None
    label_ids: tuple[str, ...]
    headers: Mapping[str, str]  # selected header names, lower-cased


@dataclass(frozen=True)
class RawMessage:
    message_id: str
    thread_id: str | None
    label_ids: tuple[str, ...]
    internal_date_ms: int
    raw: bytes


@dataclass(frozen=True)
class HistoryRecord:
    history_id: str
    added: tuple[tuple[str, tuple[str, ...]], ...]  # (message id, label ids) of messagesAdded


@dataclass(frozen=True)
class HistoryPage:
    records: tuple[HistoryRecord, ...]
    next_page_token: str | None
    history_id: str  # the mailbox's current history id


class GmailApi(Protocol):
    def profile(self) -> Profile: ...
    def send(self, raw: bytes, *, thread_id: str | None) -> SentMessage: ...
    def find_message_ids(self, query: str, *, max_results: int, include_spam_trash: bool) -> tuple[str, ...]: ...
    def get_metadata(self, message_id: str, headers: Sequence[str]) -> MessageMetadata: ...
    def get_raw(self, message_id: str) -> RawMessage: ...
    def history(self, start_history_id: str, *, page_token: str | None, max_results: int) -> HistoryPage: ...


class HttpSession(Protocol):
    def request(self, method: str, url: str, **kwargs: Any) -> Any: ...


class GmailClient:
    def __init__(self, auth: GmailAuth, *, timeout_seconds: int, session: HttpSession | None = None) -> None:
        self._auth = auth
        self._timeout = timeout_seconds
        self._session = session

    def __repr__(self) -> str:
        return "GmailClient(<redacted>)"

    # ---- Operations -----------------------------------------------------------------------

    def profile(self) -> Profile:
        data = self._call("GET", "/profile")
        return Profile(email_address=str(data["emailAddress"]).lower(), history_id=str(data["historyId"]))

    def send(self, raw: bytes, *, thread_id: str | None) -> SentMessage:
        body: dict[str, str] = {"raw": base64.urlsafe_b64encode(raw).decode("ascii")}
        if thread_id:
            body["threadId"] = thread_id
        data = self._call("POST", "/messages/send", json=body)
        if not data.get("id"):
            # Gmail answered without the id it always returns on success: not provable either way.
            raise GmailError(GmailCode.UNEXPECTED_RESPONSE, delivery=Delivery.UNKNOWN)
        return SentMessage(message_id=str(data["id"]), thread_id=_opt(data.get("threadId")))

    def find_message_ids(self, query: str, *, max_results: int, include_spam_trash: bool) -> tuple[str, ...]:
        data = self._call("GET", "/messages", params={"q": query, "maxResults": max_results,
                                                       "includeSpamTrash": str(include_spam_trash).lower()})
        return tuple(str(m["id"]) for m in data.get("messages", []) if m.get("id"))

    def get_metadata(self, message_id: str, headers: Sequence[str]) -> MessageMetadata:
        data = self._call("GET", f"/messages/{message_id}", params=[("format", "metadata"),
                                                                     *(("metadataHeaders", h) for h in headers)])
        found = {str(h.get("name", "")).lower(): str(h.get("value", "")) for h in data.get("payload", {}).get("headers", [])}
        return MessageMetadata(message_id=str(data["id"]), thread_id=_opt(data.get("threadId")),
                               label_ids=tuple(data.get("labelIds", ())), headers=found)

    def get_raw(self, message_id: str) -> RawMessage:
        data = self._call("GET", f"/messages/{message_id}", params={"format": "raw"})
        raw = str(data.get("raw", ""))
        return RawMessage(message_id=str(data["id"]), thread_id=_opt(data.get("threadId")),
                          label_ids=tuple(data.get("labelIds", ())), internal_date_ms=int(data.get("internalDate", 0)),
                          raw=base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))

    def history(self, start_history_id: str, *, page_token: str | None, max_results: int) -> HistoryPage:
        params: dict[str, object] = {"startHistoryId": start_history_id, "historyTypes": "messageAdded",
                                     "maxResults": max_results}
        if page_token:
            params["pageToken"] = page_token
        try:
            data = self._call("GET", "/history", params=params)
        except GmailError as exc:
            if exc.code is GmailCode.NOT_FOUND:  # Gmail's answer to a start id that is too old
                raise GmailError(GmailCode.HISTORY_EXPIRED, status=exc.status, delivery=Delivery.REJECTED) from None
            raise
        records = tuple(
            HistoryRecord(history_id=str(record["id"]),
                          added=tuple((str(item["message"]["id"]), tuple(item["message"].get("labelIds", ())))
                                      for item in record.get("messagesAdded", [])))
            for record in data.get("history", []))
        return HistoryPage(records=records, next_page_token=_opt(data.get("nextPageToken")),
                           history_id=str(data.get("historyId", start_history_id)))

    # ---- Transport ------------------------------------------------------------------------------

    def _session_for_call(self) -> HttpSession:
        """One session per client (one connection pool). GmailAuth returns the same
        credentials object every time and refreshes it in place, so the session always
        authorizes with the current token."""
        credentials = self._auth.credentials()  # refreshes and persists first; failure = NOT_SENT
        if self._session is None:
            from google.auth.transport.requests import AuthorizedSession

            session = AuthorizedSession(credentials, refresh_status_codes=(), max_refresh_attempts=0)
            session.headers["User-Agent"] = USER_AGENT
            self._session = session
        return self._session

    def _call(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        session = self._session_for_call()
        try:
            response = session.request(method, BASE_URL + path, timeout=self._timeout, **kwargs)
        except http.ConnectTimeout:
            raise GmailError(GmailCode.TEMPORARY_PROVIDER_ERROR, delivery=Delivery.NOT_SENT) from None
        except (http.RequestException, OSError):
            raise GmailError(GmailCode.TEMPORARY_PROVIDER_ERROR, delivery=Delivery.UNKNOWN) from None
        status = int(response.status_code)
        if status >= 400:
            raise _http_error(status, response)
        try:
            data = response.json()
        except ValueError:
            raise GmailError(GmailCode.UNEXPECTED_RESPONSE, status=status, delivery=Delivery.UNKNOWN) from None
        if not isinstance(data, dict):
            raise GmailError(GmailCode.UNEXPECTED_RESPONSE, status=status, delivery=Delivery.UNKNOWN)
        return data


def _http_error(status: int, response: Any) -> GmailError:
    if status >= 500:
        return GmailError(GmailCode.TEMPORARY_PROVIDER_ERROR, status=status, delivery=Delivery.UNKNOWN)
    code = {400: GmailCode.INVALID_REQUEST, 401: GmailCode.AUTH_REQUIRED, 403: GmailCode.PERMISSION_DENIED,
            404: GmailCode.NOT_FOUND, 429: GmailCode.RATE_LIMITED}.get(status, GmailCode.INVALID_REQUEST)
    if status == 403 and _reasons(response) & RATE_LIMIT_REASONS:
        code = GmailCode.RATE_LIMITED
    return GmailError(code, status=status, delivery=Delivery.REJECTED)


def _reasons(response: Any) -> set[str]:
    try:
        errors = response.json().get("error", {}).get("errors", [])
        return {str(e.get("reason")) for e in errors if isinstance(e, dict)}
    except Exception:  # noqa: BLE001 - only used to refine a code
        return set()


def _opt(value: object) -> str | None:
    return str(value) if value else None
