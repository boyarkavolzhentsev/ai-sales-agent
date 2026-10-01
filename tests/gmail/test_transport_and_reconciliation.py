"""Gmail transport (Stage 8 EmailTransport) and reconciler (DispatchReconciler) over the
fake Gmail API."""

from datetime import UTC, datetime
from email import message_from_bytes, policy

import pytest

from app.dispatch.transport import NotSubmittedError, ReconciliationFinding, TransportOutcome, TransportRequest
from app.integrations.gmail.errors import Delivery, GmailCode, GmailError
from app.integrations.gmail.reconciliation import GmailReconciler
from app.integrations.gmail.transport import GmailTransport
from tests.gmail.fakes import ACCOUNT, FakeGmailApi, Send, customer_email


def request(**overrides: object) -> TransportRequest:
    data: dict[str, object] = {"request_id": "da_1", "rfc_message_id": "<da_1@ourco.example>", "sender_mailbox": ACCOUNT,
                               "sender_name": "Alex Seller", "recipient": "buyer@prospect.example",
                               "subject": "Re: Pricing question", "body": "Hi, the Basic plan costs 100 EUR per month."}
    return TransportRequest.model_validate(data | overrides)


def transport(api: FakeGmailApi) -> GmailTransport:
    return GmailTransport(api, address=ACCOUNT, now=lambda: datetime(2026, 6, 1, 12, 0, tzinfo=UTC))


def sent(api: FakeGmailApi, index: int = -1):  # noqa: ANN201
    return message_from_bytes(api.sent_calls[index][0], policy=policy.default)


# ---- Transport ------------------------------------------------------------------------------------


def test_a_plain_message_is_sent_once_and_accepted() -> None:
    api = FakeGmailApi()
    result = transport(api).submit(request())
    assert (result.outcome, result.reason_code) == (TransportOutcome.ACCEPTED, "GMAIL_ACCEPTED")
    assert result.provider_message_id in api.messages and len(api.sent_calls) == 1
    message = sent(api)
    assert str(message["From"]) == "Alex Seller <sales@ourco.example>" and str(message["To"]) == "buyer@prospect.example"
    assert str(message["Message-ID"]) == "<da_1@ourco.example>" and str(message["Subject"]) == "Re: Pricing question"
    assert message.get_content_type() == "text/plain" and message.get_body().get_content().strip().endswith("per month.")
    assert message["In-Reply-To"] is None and api.sent_calls[0][1] is None


def test_utf8_names_subjects_and_bodies_survive() -> None:
    api = FakeGmailApi()
    transport(api).submit(request(sender_name="Олександр Продавець", subject="Ціна: «Basic» — 100 €", body="Привіт! Ціна 100 €."))
    message = sent(api)
    assert str(message["Subject"]) == "Ціна: «Basic» — 100 €" and message["From"].addresses[0].display_name == "Олександр Продавець"
    assert message.get_body().get_content().strip() == "Привіт! Ціна 100 €."
    assert all(byte < 128 for byte in api.sent_calls[0][0].split(b"\r\n\r\n")[0])  # headers are RFC 2047 encoded


def test_a_reply_carries_threading_headers_and_the_gmail_thread() -> None:
    api = FakeGmailApi()
    original = api.deliver(customer_email(message_id="<p-1@prospect.example>"), thread_id="t-customer")
    transport(api).submit(request(in_reply_to="<p-1@prospect.example>", references=("<p-0@prospect.example>", "<p-1@prospect.example>")))
    message = sent(api)
    assert str(message["In-Reply-To"]) == "<p-1@prospect.example>"
    assert str(message["References"]) == "<p-0@prospect.example> <p-1@prospect.example>"
    assert api.sent_calls[0][1] == api.messages[original].thread_id == "t-customer"


def test_an_unknown_reply_target_still_sends_with_headers_only() -> None:
    api = FakeGmailApi()
    api.fail["find_message_ids"] = GmailError(GmailCode.TEMPORARY_PROVIDER_ERROR, delivery=Delivery.UNKNOWN)
    result = transport(api).submit(request(in_reply_to="<gone@prospect.example>"))
    assert result.outcome is TransportOutcome.ACCEPTED and api.sent_calls[0][1] is None


@pytest.mark.parametrize(("behavior", "outcome", "retryable"), [
    (Send.REJECT_INVALID, TransportOutcome.NOT_ACCEPTED, False),
    (Send.RATE_LIMITED, TransportOutcome.NOT_ACCEPTED, True),
    (Send.SERVER_ERROR, TransportOutcome.UNKNOWN, False),
    (Send.ACCEPT_THEN_LOSE_RESPONSE, TransportOutcome.UNKNOWN, False),
])
def test_outcomes_are_mapped_conservatively_without_any_retry(behavior: Send, outcome: TransportOutcome, retryable: bool) -> None:
    api = FakeGmailApi(send_script=[behavior])
    result = transport(api).submit(request())
    assert (result.outcome, result.retryable) == (outcome, retryable) and len(api.sent_calls) == 1
    assert result.provider_message_id is None and result.reason_code.startswith("GMAIL_")


def test_nothing_sent_is_reported_as_not_submitted() -> None:
    api = FakeGmailApi(send_script=[Send.CONNECT_TIMEOUT])
    with pytest.raises(NotSubmittedError) as error:
        transport(api).submit(request())
    assert error.value.reason_code == "GMAIL_TEMPORARY_PROVIDER_ERROR"
    api.fail["send"] = GmailError(GmailCode.AUTH_REFRESH_FAILED, delivery=Delivery.NOT_SENT)
    with pytest.raises(NotSubmittedError):
        transport(api).submit(request())


def test_never_sends_from_another_mailbox_or_with_injected_headers() -> None:
    api = FakeGmailApi()
    with pytest.raises(NotSubmittedError) as other:
        transport(api).submit(request(sender_mailbox="support@ourco.example"))
    assert other.value.reason_code == "GMAIL_MAILBOX_NOT_AUTHORIZED"
    with pytest.raises(NotSubmittedError) as injected:
        transport(api).submit(request(subject="Hello\r\nBcc: victim@example.com"))
    assert injected.value.reason_code == "GMAIL_MESSAGE_NOT_BUILT"
    assert api.sent_calls == []


# ---- Reconciliation ---------------------------------------------------------------------------------


def test_reconciliation_confirms_a_sent_message_by_its_message_id() -> None:
    api = FakeGmailApi(send_script=[Send.ACCEPT_THEN_LOSE_RESPONSE])
    assert transport(api).submit(request()).outcome is TransportOutcome.UNKNOWN
    found = GmailReconciler(api).lookup("da_1", "<da_1@ourco.example>")
    assert found.finding is ReconciliationFinding.ACCEPTED and found.provider_message_id in api.messages
    assert len(api.sent_calls) == 1  # reconciliation never sends


def test_absence_is_never_a_rejection() -> None:
    api = FakeGmailApi(send_script=[Send.SERVER_ERROR])
    transport(api).submit(request())
    missing = GmailReconciler(api).lookup("da_1", "<da_1@ourco.example>")
    assert (missing.finding, missing.reason_code) == (ReconciliationFinding.NOT_FOUND, "GMAIL_NOT_FOUND")
    assert missing.as_transport_result().outcome is TransportOutcome.UNKNOWN


def test_read_errors_keep_the_attempt_unresolved() -> None:
    api = FakeGmailApi()
    api.fail["find_message_ids"] = GmailError(GmailCode.RATE_LIMITED, status=429, delivery=Delivery.REJECTED)
    result = GmailReconciler(api).lookup("da_1", "<da_1@ourco.example>")
    assert (result.finding, result.reason_code) == (ReconciliationFinding.UNKNOWN, "GMAIL_RATE_LIMITED")


def test_only_our_own_sent_copy_with_the_exact_message_id_counts() -> None:
    api = FakeGmailApi()
    # The same Message-ID on a message that is not in SENT (e.g. a copy someone forwarded back).
    api.deliver(customer_email(message_id="<da_1@ourco.example>"))
    assert GmailReconciler(api).lookup("da_1", "<da_1@ourco.example>").finding is ReconciliationFinding.NOT_FOUND
    transport(api).submit(request(rfc_message_id="<da_1x@ourco.example>"))  # a different attempt
    assert GmailReconciler(api).lookup("da_1", "<da_1@ourco.example>").finding is ReconciliationFinding.NOT_FOUND


def test_reconciliation_never_claims_a_final_rejection() -> None:
    import ast
    import inspect

    from app.integrations.gmail import reconciliation
    tree = ast.parse(inspect.getsource(reconciliation))
    assert not any(isinstance(n, ast.Attribute) and n.attr == "REJECTED_FINAL" for n in ast.walk(tree))
    assert "subject" not in inspect.getsource(reconciliation).lower().replace("subjects", "")


def test_failures_before_the_send_are_never_reported_as_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: an unexpected error in the pre-send thread lookup or MIME building would
    have surfaced as an exception (Stage 8: UNKNOWN) for a message that never left."""
    api = FakeGmailApi()
    api.fail["find_message_ids"] = KeyError("odd response")  # type: ignore[assignment]
    assert transport(api).submit(request(in_reply_to="<p-1@prospect.example>")).outcome is TransportOutcome.ACCEPTED
    from email import errors as email_errors

    from app.integrations.gmail import transport as gmail_transport

    def broken(*args: object, **kwargs: object) -> bytes:
        raise email_errors.HeaderParseError("bad header")

    monkeypatch.setattr(gmail_transport, "build_outbound", broken)
    with pytest.raises(NotSubmittedError) as error:
        transport(FakeGmailApi()).submit(request())
    assert error.value.reason_code == "GMAIL_MESSAGE_NOT_BUILT"
