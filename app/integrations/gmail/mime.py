"""MIME for Gmail: outbound plain-text messages and inbound normalization (stdlib ``email``).

Outbound (V1): From (the authorized account, display name from the sender identity), To,
Subject, Date, Message-ID (Stage 8's per-attempt id), In-Reply-To and References, one
UTF-8 text/plain body. No attachments, no Reply-To, no other sender address. Header
values with line breaks are refused by the email package (no header injection).

Inbound: the text/plain body is preferred; an HTML-only message is reduced to its text
(scripts and styles dropped, nothing fetched or executed); attachments are never used as
the customer's words. Drafts, spam, trash, chat and our own sent mail are filtered.
"""

import hashlib
from datetime import UTC, datetime
from email import message_from_bytes, policy
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import format_datetime, getaddresses, parsedate_to_datetime
from html.parser import HTMLParser

from pydantic import TypeAdapter, ValidationError

from app.core.models.types import EmailAddress
from app.dispatch.transport import TransportRequest
from app.inbound.models import InboundEnvelope
from app.integrations.gmail.client import RawMessage
from app.integrations.mailbox import FetchedMessage

PROVIDER = "gmail"
SKIP_LABELS = ("DRAFT", "SPAM", "TRASH", "SENT", "CHAT")
_ADDRESS = TypeAdapter(EmailAddress)


def build_outbound(request: TransportRequest, *, now: datetime) -> bytes:
    message = EmailMessage(policy=policy.SMTP)
    local, domain = request.sender_mailbox.split("@", 1)
    message["From"] = Address(display_name=request.sender_name, username=local, domain=domain)
    message["To"] = request.recipient
    message["Subject"] = request.subject
    message["Date"] = format_datetime(now.astimezone(UTC))
    message["Message-ID"] = request.rfc_message_id
    if request.in_reply_to:
        message["In-Reply-To"] = request.in_reply_to
    if request.references:
        message["References"] = " ".join(request.references)
    message.set_content(request.body, subtype="plain", charset="utf-8")
    return message.as_bytes()


def skip_label(label_ids: tuple[str, ...]) -> str | None:
    found = next((label for label in SKIP_LABELS if label in label_ids), None)
    return f"LABEL_{found}" if found else None


def normalize_inbound(message: RawMessage, *, mailbox: str) -> FetchedMessage:
    skip = skip_label(message.label_ids)
    if skip is not None:
        return FetchedMessage(skip_reason=skip)
    parsed = message_from_bytes(message.raw, policy=policy.default)
    sender = _first_address(parsed.get("From"))
    if sender is None:
        return FetchedMessage(skip_reason="INVALID_SENDER")
    if sender == mailbox:
        return FetchedMessage(skip_reason="SELF_SENT")  # our own mail never re-enters as customer mail
    try:
        envelope = InboundEnvelope(
            provider=PROVIDER, provider_message_id=message.message_id,
            internet_message_id=_header(parsed, "Message-ID"), mailbox=mailbox, from_address=sender,
            to_addresses=_addresses(parsed.get_all("To", [])), cc_addresses=_addresses(parsed.get_all("Cc", [])),
            subject=_header(parsed, "Subject") or "", body_text=_body(parsed),
            received_at=datetime.fromtimestamp(message.internal_date_ms / 1000, UTC), sent_at=_date(parsed),
            in_reply_to=_first_id(_header(parsed, "In-Reply-To")), references=_ids(_header(parsed, "References")),
            auto_submitted=_header(parsed, "Auto-Submitted"), precedence=_header(parsed, "Precedence"),
            x_autoreply=_header(parsed, "X-Autoreply"), content_type=_header(parsed, "Content-Type"),
            list_unsubscribe=_header(parsed, "List-Unsubscribe"), provider_thread_ref=message.thread_id,
            has_attachments=parsed.is_multipart() and any(True for _ in parsed.iter_attachments()),
            raw_ref=f"{PROVIDER}:{message.message_id}", raw_hash=hashlib.sha256(message.raw).hexdigest(),
        )
    except (ValidationError, ValueError, TypeError):
        return FetchedMessage(skip_reason="MALFORMED_MESSAGE")
    return FetchedMessage(envelope=envelope)


def _header(parsed: EmailMessage, name: str) -> str | None:
    try:
        value = parsed.get(name)
    except (ValueError, IndexError):  # a defective header the parser cannot render
        return None
    text = " ".join(str(value).split()) if value is not None else ""
    return text or None


def _valid(address: str) -> str | None:
    try:
        return _ADDRESS.validate_python(address)
    except ValidationError:
        return None


def _first_address(value: object) -> str | None:
    found = [_valid(addr) for _, addr in getaddresses([str(value)]) if addr] if value is not None else []
    return next((a for a in found if a), None)


def _addresses(values: list[object]) -> tuple[str, ...]:
    found = [_valid(addr) for _, addr in getaddresses([str(v) for v in values]) if addr]
    return tuple(dict.fromkeys(a for a in found if a))


def _ids(value: str | None) -> tuple[str, ...]:
    return tuple(dict.fromkeys(part for part in (value or "").split() if part.startswith("<") and part.endswith(">")))


def _first_id(value: str | None) -> str | None:
    ids = _ids(value)
    return ids[0] if ids else (value or None)


def _date(parsed: EmailMessage) -> datetime | None:
    raw = _header(parsed, "Date")
    if raw is None:
        return None
    try:
        value = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        return None
    return value if value.tzinfo is not None else None


def _body(parsed: EmailMessage) -> str:
    """text/plain first; else readable text from text/html; never an attachment."""
    part = parsed.get_body(preferencelist=("plain",))
    if part is not None:
        return _text(part).strip()
    html = parsed.get_body(preferencelist=("html",))
    return _strip_html(_text(html)).strip() if html is not None else ""


def _text(part: EmailMessage) -> str:
    try:
        return str(part.get_content())
    except (LookupError, ValueError, AssertionError):  # unknown charset or a broken transfer encoding
        payload = part.get_payload(decode=True)
        return payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else ""


class _Text(HTMLParser):
    SKIP = frozenset({"script", "style", "head", "title"})
    BREAK = frozenset({"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP:
            self._skipping += 1
        elif tag in self.BREAK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP and self._skipping:
            self._skipping -= 1
        elif tag in self.BREAK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            self.parts.append(data)


def _strip_html(html: str) -> str:
    parser = _Text()
    parser.feed(html)
    parser.close()
    lines = (" ".join(line.split()) for line in "".join(parser.parts).splitlines())
    return "\n".join(line for line in lines if line)
