"""E. Transport outcomes. F. Crash points and recovery. H. Retries."""

import pytest

from app.core.enums import DNCReason, DNCScope, OutboundStatus, RefKind
from app.core.models import DoNotContactEntry, EntityRef
from app.dispatch import (
    DispatchCode,
    DispatchOutcome,
    FakeBehavior,
    FakeEmailTransport,
    FakeReconciler,
    FakeStep,
    TransportRequest,
)
from app.dispatch import service as dispatch_module
from app.persistence import Database, DispatchAttemptState, QuotaReservationState
from app.policy import PolicyReason
from tests.dispatch.builders import approved_reply, dispatcher, request, send, state
from tests.inbound.builders import NOW, SENDER


class SimulatedCrash(BaseException):
    """The process dies: not an Exception, so nothing in the service may catch it."""


def crash(_: TransportRequest) -> None:
    raise SimulatedCrash()


# ---- E. Transport outcomes -------------------------------------------------------------------


def test_confirmed_rejection_marks_failed_and_keeps_history(db: Database) -> None:
    outbound_id = approved_reply(db)
    transport = FakeEmailTransport().script(FakeStep(FakeBehavior.REJECT, retryable=False))
    result = send(dispatcher(db, transport), outbound_id)
    assert (result.outcome, result.outbound_status) == (DispatchOutcome.NOT_ACCEPTED, OutboundStatus.FAILED)
    [attempt] = state(db, outbound_id).attempts
    assert (attempt.state, attempt.reason_code, attempt.retryable) == (DispatchAttemptState.NOT_ACCEPTED, "PROVIDER_REJECTED", False)
    assert state(db, outbound_id).outbound.failure_reason == "PROVIDER_REJECTED"


def test_failure_before_submission_is_a_retryable_non_acceptance(db: Database) -> None:
    outbound_id = approved_reply(db)
    result = send(dispatcher(db, FakeEmailTransport().script(FakeBehavior.FAIL_BEFORE_SUBMIT)), outbound_id)
    assert result.outcome is DispatchOutcome.NOT_ACCEPTED and result.reason_codes == ("CONNECTION_REFUSED",)
    assert state(db, outbound_id).attempts[0].retryable


@pytest.mark.parametrize("behavior", [FakeBehavior.TIMEOUT, FakeBehavior.ACCEPT_THEN_LOSE_RESPONSE, FakeBehavior.UNKNOWN_RESULT])
def test_unknown_outcomes_stay_unresolved_and_are_never_resent(db: Database, behavior: FakeBehavior) -> None:
    outbound_id = approved_reply(db)
    transport = FakeEmailTransport().script(behavior)
    result = send(dispatcher(db, transport), outbound_id)
    assert (result.outcome, result.outbound_status) == (DispatchOutcome.UNKNOWN, OutboundStatus.SENDING)
    assert all("fake" not in code for code in result.reason_codes)  # stable codes, no raw messages
    [attempt] = state(db, outbound_id).attempts
    assert attempt.state is DispatchAttemptState.UNKNOWN
    again = send(dispatcher(db, transport), outbound_id, "corr-retry")
    assert again.outcome is DispatchOutcome.UNKNOWN and DispatchCode.ATTEMPT_UNRESOLVED in again.reason_codes
    assert len(transport.calls) == 1 and not again.transport_called


def test_accepted_but_response_lost_is_found_by_reconciliation(db: Database) -> None:
    outbound_id = approved_reply(db)
    transport = FakeEmailTransport().script(FakeBehavior.ACCEPT_THEN_LOSE_RESPONSE)
    send(dispatcher(db, transport), outbound_id)
    reconciler = FakeReconciler(transport)
    result = dispatcher(db, transport, reconciler=reconciler).reconcile(request(outbound_id, "corr-rec"))
    assert (result.outcome, result.outbound_status, result.reconciled) == (DispatchOutcome.ACCEPTED, OutboundStatus.SENT, True)
    assert result.provider_message_id == "fake-1" and len(transport.calls) == 1


# ---- F. Crash points and recovery --------------------------------------------------------------


def test_crash_before_the_claim_commits_leaves_nothing(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    outbound_id = approved_reply(db)

    def dies(*args: object, **kwargs: object) -> None:
        raise SimulatedCrash()

    monkeypatch.setattr(dispatch_module, "consume_reservation", dies)
    transport = FakeEmailTransport()
    with pytest.raises(SimulatedCrash):
        send(dispatcher(db, transport), outbound_id)
    current = state(db, outbound_id)
    assert current.outbound.status is OutboundStatus.OPERATOR_APPROVED and current.attempts == [] and current.reservations == []
    monkeypatch.undo()
    assert send(dispatcher(db, transport), outbound_id).outcome is DispatchOutcome.ACCEPTED and len(transport.calls) == 1


def test_crash_after_claim_before_submission_is_not_blindly_resent(db: Database) -> None:
    outbound_id = approved_reply(db)
    transport = FakeEmailTransport().script(FakeStep(FakeBehavior.ACCEPT, before=crash))
    with pytest.raises(SimulatedCrash):
        send(dispatcher(db, transport), outbound_id)
    transport.accepted.clear()  # the crash happened before the provider saw anything
    assert state(db, outbound_id).attempts[0].state is DispatchAttemptState.CLAIMED

    restarted = dispatcher(db, transport)  # a new process: no memory of the attempt
    assert send(restarted, outbound_id, "corr-restart").outcome is DispatchOutcome.UNKNOWN
    assert len(transport.calls) == 1
    assert [v.outbound_id for v in restarted.list_unresolved()] == [outbound_id]

    # Without a reconciler the attempt stays unresolved for an operator; nothing is invented.
    kept = restarted.reconcile(request(outbound_id, "corr-rec-1"))
    assert kept.outcome is DispatchOutcome.UNKNOWN and DispatchCode.RECONCILIATION_UNAVAILABLE in kept.reason_codes
    # The provider has no record of it: absence is not proof of non-acceptance, so the
    # attempt stays unresolved and no retry is possible.
    looked_up = dispatcher(db, transport, reconciler=FakeReconciler(transport)).reconcile(request(outbound_id, "corr-rec-2"))
    assert (looked_up.outcome, looked_up.outbound_status) == (DispatchOutcome.UNKNOWN, OutboundStatus.SENDING)
    assert send(dispatcher(db, transport), outbound_id, "corr-retry").reason_codes == (DispatchCode.ATTEMPT_UNRESOLVED,)
    assert len(transport.calls) == 1 and [v.outbound_id for v in restarted.list_unresolved()] == [outbound_id]


def test_crash_after_acceptance_before_the_result_is_persisted(db: Database) -> None:
    outbound_id = approved_reply(db)

    def accept_then_die(req: TransportRequest) -> None:
        transport.accepted[req.rfc_message_id] = "fake-accepted"
        raise SimulatedCrash()

    transport = FakeEmailTransport()
    transport.script(FakeStep(FakeBehavior.ACCEPT, before=accept_then_die))
    with pytest.raises(SimulatedCrash):
        send(dispatcher(db, transport), outbound_id)
    assert state(db, outbound_id).outbound.status is OutboundStatus.SENDING
    assert send(dispatcher(db, transport), outbound_id, "corr-restart").transport_called is False

    reconciler = FakeReconciler(transport)
    service = dispatcher(db, transport, reconciler=reconciler)
    first = service.reconcile(request(outbound_id, "corr-rec"))
    second = service.reconcile(request(outbound_id, "corr-rec-again"))
    assert first.outcome is second.outcome is DispatchOutcome.ACCEPTED and first.provider_message_id == "fake-accepted"
    assert second.replayed and len(reconciler.lookups) == 1  # idempotent: resolved once
    assert len(transport.calls) == 1


def test_failure_during_result_finalization_never_resubmits(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    outbound_id = approved_reply(db)
    transport = FakeEmailTransport()
    original = dispatch_module.DispatchService._mark_sent

    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(dispatch_module.DispatchService, "_mark_sent", broken)
    result = send(dispatcher(db, transport), outbound_id)
    assert result.outcome is DispatchOutcome.UNKNOWN and DispatchCode.FINALIZATION_FAILED in result.reason_codes
    [attempt] = state(db, outbound_id).attempts
    assert (attempt.state, attempt.reason_code) == (DispatchAttemptState.UNKNOWN, DispatchCode.FINALIZATION_FAILED)

    monkeypatch.setattr(dispatch_module.DispatchService, "_mark_sent", original)
    assert send(dispatcher(db, transport), outbound_id, "corr-again").outcome is DispatchOutcome.UNKNOWN
    recovered = dispatcher(db, transport, reconciler=FakeReconciler(transport)).reconcile(request(outbound_id, "corr-rec"))
    assert recovered.outcome is DispatchOutcome.ACCEPTED and len(transport.calls) == 1


def test_crash_during_finalization_keeps_the_attempt_claimed(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    outbound_id = approved_reply(db)

    def dies(*args: object, **kwargs: object) -> None:
        raise SimulatedCrash()

    monkeypatch.setattr(dispatch_module.DispatchService, "_mark_sent", dies)
    transport = FakeEmailTransport()
    with pytest.raises(SimulatedCrash):
        send(dispatcher(db, transport), outbound_id)
    monkeypatch.undo()
    assert state(db, outbound_id).attempts[0].state is DispatchAttemptState.CLAIMED
    assert send(dispatcher(db, transport), outbound_id, "corr-restart").transport_called is False
    assert len(transport.calls) == 1


# ---- H. Retries -------------------------------------------------------------------------------


def test_retry_after_confirmed_non_acceptance_is_a_new_attempt(db: Database) -> None:
    outbound_id = approved_reply(db)
    transport = FakeEmailTransport().script(FakeBehavior.FAIL_BEFORE_SUBMIT, FakeBehavior.ACCEPT)
    first = send(dispatcher(db, transport), outbound_id)
    second = send(dispatcher(db, transport), outbound_id, "corr-retry")
    assert (first.outcome, second.outcome) == (DispatchOutcome.NOT_ACCEPTED, DispatchOutcome.ACCEPTED)
    attempts = state(db, outbound_id).attempts
    assert [a.state for a in attempts] == [DispatchAttemptState.NOT_ACCEPTED, DispatchAttemptState.ACCEPTED]
    assert len({a.attempt_id for a in attempts}) == len({a.permit.permit_id for a in attempts}) == 2
    assert len({a.rfc_message_id for a in attempts}) == 2 and {a.content_hash for a in attempts} == {attempts[0].permit.content_hash}
    assert first.reservation_id == second.reservation_id  # the same message is accounted once
    assert [c.request_id for c in transport.calls] == [a.attempt_id for a in attempts]


def test_no_retry_after_a_permanent_rejection_acceptance_or_limit(db: Database) -> None:
    permanent = approved_reply(db, "p-1")
    transport = FakeEmailTransport().script(FakeStep(FakeBehavior.REJECT, retryable=False))
    send(dispatcher(db, transport), permanent)
    assert send(dispatcher(db, transport), permanent, "c2").reason_codes == (DispatchCode.RETRY_NOT_PERMITTED,)

    limited = approved_reply(db, "p-2", sender="other@elsewhere.example")
    transport = FakeEmailTransport().script(*[FakeBehavior.FAIL_BEFORE_SUBMIT] * 3)
    for index in range(3):
        send(dispatcher(db, transport), limited, f"c-{index}")
    assert send(dispatcher(db, transport), limited, "c-last").reason_codes == (DispatchCode.RETRY_LIMIT_REACHED,)
    assert len(transport.calls) == 3


def test_retry_revalidates_every_gate(db: Database) -> None:
    outbound_id = approved_reply(db)
    transport = FakeEmailTransport().script(FakeBehavior.FAIL_BEFORE_SUBMIT)
    send(dispatcher(db, transport), outbound_id)
    with db.transaction() as uow:
        uow.dnc.add(DoNotContactEntry(entry_id="dnc-1", scope=DNCScope.EMAIL, value=SENDER, reason=DNCReason.OPERATOR,
                                      source_ref=EntityRef(kind=RefKind.OPERATOR_COMMAND, id="c"), created_by="op", created_at=NOW))
    retry = send(dispatcher(db, transport), outbound_id, "corr-retry")
    assert retry.outcome is DispatchOutcome.BLOCKED and PolicyReason.DNC_EMAIL.value in retry.reason_codes
    assert len(transport.calls) == 1 and state(db, outbound_id).outbound.status is OutboundStatus.FAILED


def test_accounting_of_a_failed_then_retried_message(db: Database) -> None:
    outbound_id = approved_reply(db)
    transport = FakeEmailTransport().script(FakeBehavior.FAIL_BEFORE_SUBMIT, FakeBehavior.ACCEPT)
    send(dispatcher(db, transport), outbound_id)
    send(dispatcher(db, transport), outbound_id, "corr-retry")
    [reservation] = state(db, outbound_id).reservations
    assert reservation.state is QuotaReservationState.CONSUMED and reservation.version == 2  # consumed once
