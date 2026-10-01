"""A fake Gmail API beneath the adapters (implements ``GmailApi``): no network, no Google
library. It keeps a provider-side truth (messages with labels, history records, the
account's history id) so tests can model lost responses, late acceptance, expiry, etc."""

import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email import message_from_bytes, policy
from email.message import EmailMessage
from enum import StrEnum

from app.integrations.gmail.client import HistoryPage, HistoryRecord, MessageMetadata, Profile, RawMessage, SentMessage
from app.integrations.gmail.errors import Delivery, GmailCode, GmailError

ACCOUNT = "sales@ourco.example"


class Send(StrEnum):
    ACCEPT = "ACCEPT"
    ACCEPT_THEN_LOSE_RESPONSE = "ACCEPT_THEN_LOSE_RESPONSE"  # Gmail stored it, the answer was lost
    SERVER_ERROR = "SERVER_ERROR"  # 5xx: not stored here, but the caller cannot know
    REJECT_INVALID = "REJECT_INVALID"  # 400
    RATE_LIMITED = "RATE_LIMITED"  # 429
    CONNECT_TIMEOUT = "CONNECT_TIMEOUT"  # nothing left the process


@dataclass
class StoredMessage:
    message_id: str
    thread_id: str
    labels: tuple[str, ...]
    internal_ms: int
    raw: bytes

    @property
    def headers(self) -> EmailMessage:
        return message_from_bytes(self.raw, policy=policy.default)  # type: ignore[return-value]


@dataclass
class FakeGmailApi:
    address: str = ACCOUNT
    history_id: int = 1000
    messages: dict[str, StoredMessage] = field(default_factory=dict)
    history_records: list[HistoryRecord] = field(default_factory=list)
    send_script: list[Send] = field(default_factory=list)
    sent_calls: list[tuple[bytes, str | None]] = field(default_factory=list)
    expired_before: int | None = None  # history start ids below this are "too old"
    fail: dict[str, GmailError] = field(default_factory=dict)  # method name -> error (raised every call)
    fail_once: dict[str, list[GmailError]] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _ids: int = 0

    # ---- Test helpers -------------------------------------------------------------------------

    def _next_id(self, prefix: str) -> str:
        self._ids += 1
        return f"{prefix}{self._ids:05d}"

    def _record(self, message_id: str, labels: tuple[str, ...]) -> None:
        self.history_id += 1
        self.history_records.append(HistoryRecord(history_id=str(self.history_id), added=((message_id, labels),)))

    def deliver(self, raw: bytes, *, labels: tuple[str, ...] = ("INBOX", "UNREAD"), thread_id: str | None = None,
                at: datetime | None = None, record_twice: bool = False) -> str:
        """A message arriving in the mailbox (one messageAdded history record)."""
        with self._lock:
            message_id = self._next_id("m")
            internal = int((at or datetime(2026, 6, 1, 12, 5, tzinfo=UTC)).timestamp() * 1000)
            self.messages[message_id] = StoredMessage(message_id, thread_id or self._next_id("t"), labels, internal, raw)
            self._record(message_id, labels)
            if record_twice:  # e.g. a label change that Gmail reports as another messageAdded
                self._record(message_id, labels)
        return message_id

    def _check(self, method: str) -> None:
        self.calls.append(method)
        if method in self.fail:
            raise self.fail[method]
        queue = self.fail_once.get(method)
        if queue:
            raise queue.pop(0)

    # ---- GmailApi --------------------------------------------------------------------------------

    def profile(self) -> Profile:
        self._check("profile")
        return Profile(email_address=self.address, history_id=str(self.history_id))

    def send(self, raw: bytes, *, thread_id: str | None) -> SentMessage:
        self._check("send")
        with self._lock:
            self.sent_calls.append((raw, thread_id))
            behavior = self.send_script.pop(0) if self.send_script else Send.ACCEPT
        if behavior is Send.CONNECT_TIMEOUT:
            raise GmailError(GmailCode.TEMPORARY_PROVIDER_ERROR, delivery=Delivery.NOT_SENT)
        if behavior is Send.SERVER_ERROR:
            raise GmailError(GmailCode.TEMPORARY_PROVIDER_ERROR, status=503, delivery=Delivery.UNKNOWN)
        if behavior is Send.REJECT_INVALID:
            raise GmailError(GmailCode.INVALID_REQUEST, status=400, delivery=Delivery.REJECTED)
        if behavior is Send.RATE_LIMITED:
            raise GmailError(GmailCode.RATE_LIMITED, status=429, delivery=Delivery.REJECTED)
        with self._lock:
            message_id = self._next_id("s")
            thread = thread_id or self._next_id("t")
            self.messages[message_id] = StoredMessage(message_id, thread, ("SENT",), 0, raw)
            self._record(message_id, ("SENT",))
        if behavior is Send.ACCEPT_THEN_LOSE_RESPONSE:
            raise GmailError(GmailCode.TEMPORARY_PROVIDER_ERROR, delivery=Delivery.UNKNOWN)
        return SentMessage(message_id=message_id, thread_id=thread)

    def find_message_ids(self, query: str, *, max_results: int, include_spam_trash: bool) -> tuple[str, ...]:
        self._check("find_message_ids")
        assert query.startswith("rfc822msgid:"), "the fake only supports exact Message-ID queries"
        wanted = query.removeprefix("rfc822msgid:").strip("<>").lower()
        found = [m.message_id for m in self.messages.values()
                 if str(m.headers.get("Message-ID", "")).strip("<> ").lower() == wanted
                 and (include_spam_trash or not {"SPAM", "TRASH"} & set(m.labels))]
        return tuple(found[:max_results])

    def get_metadata(self, message_id: str, headers: Sequence[str]) -> MessageMetadata:
        self._check("get_metadata")
        stored = self._get(message_id)
        parsed = stored.headers
        return MessageMetadata(message_id=message_id, thread_id=stored.thread_id, label_ids=stored.labels,
                               headers={h.lower(): str(parsed.get(h, "")) for h in headers if parsed.get(h) is not None})

    def get_raw(self, message_id: str) -> RawMessage:
        self._check("get_raw")
        stored = self._get(message_id)
        return RawMessage(message_id=message_id, thread_id=stored.thread_id, label_ids=stored.labels,
                          internal_date_ms=stored.internal_ms, raw=stored.raw)

    def history(self, start_history_id: str, *, page_token: str | None, max_results: int) -> HistoryPage:
        self._check("history")
        start = int(start_history_id)
        if self.expired_before is not None and start < self.expired_before:
            raise GmailError(GmailCode.HISTORY_EXPIRED, status=404, delivery=Delivery.REJECTED)
        after = [r for r in self.history_records if int(r.history_id) > start]
        offset = int(page_token or 0)
        page = after[offset:offset + max_results]
        more = offset + max_results < len(after)
        return HistoryPage(records=tuple(page), next_page_token=str(offset + max_results) if more else None,
                           history_id=str(self.history_id))

    def _get(self, message_id: str) -> StoredMessage:
        if message_id not in self.messages:
            raise GmailError(GmailCode.NOT_FOUND, status=404, delivery=Delivery.REJECTED)
        return self.messages[message_id]


def customer_email(*, sender: str = "buyer@prospect.example", body: str = "Hi, how much does the Basic plan cost per month?",
                   subject: str = "Pricing question", message_id: str = "<p-1@prospect.example>",
                   in_reply_to: str | None = None, to: str = ACCOUNT, html: str | None = None,
                   attachment: tuple[str, bytes] | None = None, extra: dict[str, str] | None = None) -> bytes:
    message = EmailMessage(policy=policy.SMTP)
    message["From"] = sender
    message["To"] = to
    message["Subject"] = subject
    message["Date"] = "Mon, 01 Jun 2026 12:05:00 +0000"
    if message_id:
        message["Message-ID"] = message_id
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
        message["References"] = in_reply_to
    for name, value in (extra or {}).items():
        message[name] = value
    if body is not None:
        message.set_content(body, charset="utf-8")
    if html is not None:
        if body is None:
            message.set_content(html, subtype="html", charset="utf-8")
        else:
            message.add_alternative(html, subtype="html", charset="utf-8")
    if attachment is not None:
        name, data = attachment
        message.add_attachment(data, maintype="text", subtype="plain", filename=name)
    return message.as_bytes()
