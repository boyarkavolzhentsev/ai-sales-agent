"""Gmail implementation of Stage 8's ``EmailTransport``.

Stage 8 has already decided the message may be sent (approval, gates, policy, quota,
permit) and owns idempotency: one ``submit`` call is exactly one ``users.messages.send``
request, never retried here. Outcome mapping (conservative):
  Gmail returned the new message id                        -> ACCEPTED (not proof of delivery)
  nothing left the process (MIME refused, refresh failed,   -> NotSubmittedError (Stage 8: NOT_ACCEPTED,
    connect timeout, wrong sender mailbox)                     retryable by Stage 8's own rules)
  Gmail answered with an error (4xx)                       -> NOT_ACCEPTED (retryable only for rate
                                                              limits and an expired authorization)
  anything else (5xx, read timeout, lost response, odd body) -> UNKNOWN: reconcile, never resend
The sender is always the authorized account: a request for any other mailbox is refused.
"""

from collections.abc import Callable
from datetime import UTC, datetime

from app.dispatch.transport import NotSubmittedError, TransportOutcome, TransportRequest, TransportResult
from app.integrations.gmail.client import GmailApi
from app.integrations.gmail.errors import Delivery, GmailCode, GmailError
from app.integrations.gmail.mime import build_outbound

RETRYABLE = frozenset({GmailCode.RATE_LIMITED, GmailCode.AUTH_REQUIRED})


class GmailTransport:
    def __init__(self, api: GmailApi, *, address: str, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._api = api
        self._address = address
        self._now = now

    def submit(self, request: TransportRequest) -> TransportResult:
        if request.sender_mailbox != self._address:
            raise NotSubmittedError("GMAIL_MAILBOX_NOT_AUTHORIZED")  # never send from another account
        try:
            raw = build_outbound(request, now=self._now())
        except Exception:  # noqa: BLE001 - nothing was sent: never let Stage 8 record UNKNOWN for it
            raise NotSubmittedError("GMAIL_MESSAGE_NOT_BUILT") from None
        thread_id = self._thread_of(request.in_reply_to)
        try:
            sent = self._api.send(raw, thread_id=thread_id)
        except GmailError as exc:
            reason = f"GMAIL_{exc.code.value}"
            if exc.delivery is Delivery.NOT_SENT:
                raise NotSubmittedError(reason) from None
            if exc.delivery is Delivery.REJECTED:
                return TransportResult(outcome=TransportOutcome.NOT_ACCEPTED, reason_code=reason, retryable=exc.code in RETRYABLE)
            return TransportResult(outcome=TransportOutcome.UNKNOWN, reason_code=reason)
        return TransportResult(outcome=TransportOutcome.ACCEPTED, reason_code="GMAIL_ACCEPTED", provider_message_id=sent.message_id)

    def _thread_of(self, in_reply_to: str | None) -> str | None:
        """The Gmail thread of the message we answer, so the reply stays in it in our own
        mailbox. Read-only and best effort: the In-Reply-To/References headers carry the
        threading for the recipient either way, and Stage 6 thread identity never depends
        on Gmail thread ids."""
        if not in_reply_to:
            return None
        try:
            ids = self._api.find_message_ids(f"rfc822msgid:{in_reply_to.strip('<>')}", max_results=1, include_spam_trash=False)
            return self._api.get_metadata(ids[0], ("Message-ID",)).thread_id if ids else None
        except Exception:  # noqa: BLE001 - before the send: a lookup problem must never fail or blur it
            return None
