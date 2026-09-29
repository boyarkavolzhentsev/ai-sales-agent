"""A. Full offline flow. B. Approval provenance and exact binding."""

import pytest

from app.core.enums import EmailDirection, LeadIntent, OutboundDecision, OutboundStatus, RefKind
from app.core.models import EntityRef, OutboundMessage
from app.dispatch import DispatchCode, DispatchNotFoundError, DispatchOutcome, FakeEmailTransport
from app.llm import LLMTask
from app.llm.claim_check import draft_hash
from app.persistence import Database, DispatchAttemptState, QuotaReservationState
from app.persistence.serialization import dumps_json
from tests.dispatch.builders import approve, approved_reply, dispatcher, drafted, send, snapshot, state
from tests.inbound.builders import MAILBOX, NOW, SENDER, ScriptedTransport, classification, envelope, process
from tests.operator.builders import AS_ALICE, operator, reject_command


def test_inbound_to_accepted_dispatch_end_to_end(db: Database) -> None:
    outbound_id = approved_reply(db)  # inbound email -> grounded draft -> operator approval
    transport = FakeEmailTransport()
    result = send(dispatcher(db, transport), outbound_id)

    assert result.outcome is DispatchOutcome.ACCEPTED and result.outbound_status is OutboundStatus.SENT
    assert result.transport_called and not result.replayed and result.recipient == SENDER
    [call] = transport.calls
    stored = state(db, outbound_id)
    assert (call.recipient, call.sender_mailbox, call.subject, call.body) == (
        SENDER, MAILBOX, stored.outbound.subject, stored.outbound.body_final,
    )
    assert call.in_reply_to == "<p-1@prospect.example>" and call.request_id == result.attempt_id

    sent = stored.outbound
    assert sent.status is OutboundStatus.SENT and sent.decision is OutboundDecision.SEND
    assert sent.sent_at == NOW and sent.provider_message_id == result.provider_message_id == "fake-1"
    assert sent.send_permit_id == result.permit_id and sent.rfc_message_id == call.rfc_message_id
    [attempt] = stored.attempts
    assert attempt.state is DispatchAttemptState.ACCEPTED and attempt.permit.consumed_at == NOW
    assert attempt.permit.content_hash == sent.content_hash == draft_hash(sent.subject, sent.body_final)
    [reservation] = stored.reservations
    assert reservation.state is QuotaReservationState.CONSUMED and reservation.reservation_id == result.reservation_id

    with db.transaction() as uow:
        thread = uow.threads.get(sent.thread_id or "")
        assert thread is not None
        ours = uow.messages.get(thread.message_ids[-1])
        assert ours is not None and ours.direction is EmailDirection.OUTBOUND and ours.rfc_message_id == call.rfc_message_id
        events = uow.audit.list_for_subject(EntityRef(kind=RefKind.OUTBOUND_MESSAGE, id=outbound_id))
    types = [e.event_type for e in events]
    assert "DISPATCH_CLAIMED" in types and "DISPATCH_ACCEPTED" in types
    audit_text = dumps_json([e.after for e in events if e.event_type.startswith("DISPATCH")])
    assert "100 EUR" not in audit_text and "Basic plan" not in audit_text


def test_the_customer_reply_to_our_message_threads_normally(db: Database) -> None:
    outbound_id = approved_reply(db)
    result = send(dispatcher(db), outbound_id)
    call_rfc = state(db, outbound_id).outbound.rfc_message_id
    follow = process(db, ScriptedTransport().script(LLMTask.INTENT_CLASSIFICATION, classification(LeadIntent.NEGOTIATION)),
                     envelope("p-2", body="Can you do better?", in_reply_to=call_rfc))
    assert follow.thread_id == state(db, outbound_id).outbound.thread_id and result.outcome is DispatchOutcome.ACCEPTED


def test_successful_dispatch_replays_without_another_submission(db: Database) -> None:
    outbound_id = approved_reply(db)
    transport = FakeEmailTransport()
    first = send(dispatcher(db, transport), outbound_id)
    again = send(dispatcher(db, transport), outbound_id, "corr-again")
    assert len(transport.calls) == 1
    assert again.replayed and not again.transport_called and again.outcome is DispatchOutcome.ACCEPTED
    assert (again.attempt_id, again.provider_message_id) == (first.attempt_id, first.provider_message_id)
    assert DispatchCode.ALREADY_ACCEPTED in again.reason_codes


# ---- B. Authorization and binding -------------------------------------------------------------


def assert_blocked(db: Database, outbound_id: str, code: str) -> None:
    transport = FakeEmailTransport()
    before = snapshot(db)
    result = send(dispatcher(db, transport), outbound_id)
    assert result.outcome is DispatchOutcome.BLOCKED and code in result.reason_codes, result
    assert transport.calls == [] and result.permit_id is None and result.attempt_id is None
    assert snapshot(db) == before  # no permit, reservation, attempt or status change


def test_unapproved_draft_is_not_dispatched(db: Database) -> None:
    assert_blocked(db, drafted(db), DispatchCode.NOT_OPERATOR_APPROVED)


def test_status_alone_is_not_proof_of_approval(db: Database) -> None:
    outbound_id = drafted(db)
    with db.transaction() as uow:
        current = uow.outbound.get(outbound_id)
        assert current is not None
        uow.outbound.update(OutboundMessage.model_validate(current.model_dump() | {
            "status": OutboundStatus.OPERATOR_APPROVED, "decision": OutboundDecision.SEND, "approved_at": NOW, "version": 2,
        }), 1)
    assert_blocked(db, outbound_id, DispatchCode.APPROVAL_MISSING)


def test_content_changed_after_approval_is_rejected(db: Database) -> None:
    outbound_id = approved_reply(db)
    current = state(db, outbound_id).outbound
    body = "The Basic plan costs 10 EUR per month."
    with db.transaction() as uow:
        # Even with a consistent new hash, the approval covered different content.
        uow.outbound.update(current.model_copy(update={
            "body_final": body, "content_hash": draft_hash(current.subject, body), "version": current.version + 1,
        }), current.version)
    assert_blocked(db, outbound_id, DispatchCode.APPROVAL_MISMATCH)


def test_stored_content_not_matching_its_hash_is_rejected(db: Database) -> None:
    outbound_id = approved_reply(db)
    current = state(db, outbound_id).outbound
    with db.transaction() as uow:
        uow.outbound.update(current.model_copy(update={"subject": "Re: Different", "version": current.version + 1}), current.version)
    assert_blocked(db, outbound_id, DispatchCode.CONTENT_INTEGRITY_FAILED)


def test_recipient_substitution_is_rejected(db: Database) -> None:
    outbound_id = approved_reply(db)
    contact_id = state(db, outbound_id).outbound.contact_id
    with db.transaction() as uow:
        contact = uow.contacts.get(contact_id)
        assert contact is not None
        uow.contacts.update(contact.model_copy(update={"email": "attacker@evil.example", "version": contact.version + 1}), contact.version)
    assert_blocked(db, outbound_id, DispatchCode.RECIPIENT_MISMATCH)


def test_rejected_and_cancelled_artifacts_are_never_reactivated(db: Database) -> None:
    rejected = drafted(db, "p-1")
    service = operator(db)
    service.reject_draft(AS_ALICE, reject_command(service.get_draft(AS_ALICE, rejected)))
    assert_blocked(db, rejected, DispatchCode.ARTIFACT_CANCELLED)

    cancelled = approved_reply(db, "p-2", sender="other@elsewhere.example")
    process(db, ScriptedTransport().script(LLMTask.INTENT_CLASSIFICATION, classification(LeadIntent.UNSUBSCRIBE)),
            envelope("p-3", sender="other@elsewhere.example", body="Please unsubscribe me."))
    assert_blocked(db, cancelled, DispatchCode.ARTIFACT_CANCELLED)


def test_unknown_or_non_reply_reference_is_not_found(db: Database) -> None:
    with pytest.raises(DispatchNotFoundError):
        send(dispatcher(db), "ob_missing")


def test_blocked_requests_are_audited_once_per_request(db: Database) -> None:
    outbound_id = drafted(db)
    service = dispatcher(db)
    send(service, outbound_id)
    send(service, outbound_id)
    with db.transaction() as uow:
        events = uow.audit.list_for_subject(EntityRef(kind=RefKind.OUTBOUND_MESSAGE, id=outbound_id))
    assert [e.event_type for e in events].count("DISPATCH_BLOCKED") == 1


def test_approval_of_a_different_draft_does_not_authorize_this_one(db: Database) -> None:
    first = drafted(db, "p-1")
    second = drafted(db, "p-2", sender="other@elsewhere.example")
    approve(db, first)
    with db.transaction() as uow:
        current = uow.outbound.get(second)
        assert current is not None
        uow.outbound.update(OutboundMessage.model_validate(current.model_dump() | {
            "status": OutboundStatus.OPERATOR_APPROVED, "decision": OutboundDecision.SEND, "approved_at": NOW, "version": 2,
        }), 1)
    assert_blocked(db, second, DispatchCode.APPROVAL_MISSING)
