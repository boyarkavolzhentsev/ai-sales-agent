"""Stage 8 targeted review: retries across a policy-date boundary, and reconciliation while
the original submission is still in flight."""

import threading
from datetime import datetime, timedelta
from pathlib import Path

from app.core.enums import OutboundStatus, RefKind
from app.core.models import EntityRef
from app.dispatch import (
    DispatchCode,
    DispatchOutcome,
    DispatchResult,
    FakeBehavior,
    FakeEmailTransport,
    FakeReconciler,
    FakeStep,
    TransportOutcome,
    TransportRequest,
    TransportResult,
)
from app.persistence import Database, DispatchAttemptState, FrozenClock, QuotaReservationState
from app.policy import COUNTED_STATUSES, PolicyReason, build_quota_snapshot
from app.policy.windows import local_date, local_day_bounds_utc
from tests.dispatch.builders import approved_reply, dispatcher, one_slot, request, send, state
from tests.inbound.builders import MAILBOX, NOW
from tests.policy import builders as policy

DAY_D, DAY_D1 = NOW, NOW + timedelta(days=1)  # Monday and Tuesday, 15:00 in Kyiv
OTHER = "other@elsewhere.example"


def sends_on(db: Database, day: datetime) -> int:
    """Stage 3 usage for the local policy date of ``day`` (ledger + ACTIVE reservations)."""
    tz = policy.TZ
    start, end = local_day_bounds_utc(local_date(day, tz), tz)
    with db.transaction() as uow:
        entries = uow.outbound.list_ledger_entries(COUNTED_STATUSES, start, end)
        reservations = uow.quota_reservations.list_active_for_date(local_date(day, tz))
    return build_quota_snapshot(entries, reservations, now=day, timezone=tz, mailbox=MAILBOX, campaign_id=None,
                                contact_id="nobody").sends_today


# ---- 1. Retry across a policy-date boundary ---------------------------------------------------


def test_cross_day_retry_cannot_bypass_the_new_days_limit(db: Database) -> None:
    first = approved_reply(db, "p-1")
    second = approved_reply(db, "p-2", sender=OTHER)
    transport = FakeEmailTransport().script(FakeBehavior.FAIL_BEFORE_SUBMIT, FakeBehavior.ACCEPT)
    day_d, day_d1 = FrozenClock(DAY_D), FrozenClock(DAY_D1)

    assert send(dispatcher(db, transport, clock=day_d, limits=one_slot()), first).outcome is DispatchOutcome.NOT_ACCEPTED
    assert (sends_on(db, DAY_D), sends_on(db, DAY_D1)) == (1, 0)
    assert send(dispatcher(db, transport, clock=day_d1, limits=one_slot()), second).outcome is DispatchOutcome.ACCEPTED
    assert (sends_on(db, DAY_D), sends_on(db, DAY_D1)) == (1, 1)

    retry = send(dispatcher(db, transport, clock=day_d1, limits=one_slot()), first, "corr-retry")
    assert retry.outcome is DispatchOutcome.BLOCKED and retry.reason_codes == (PolicyReason.GLOBAL_DAILY_LIMIT.value,)
    assert len(transport.calls) == 2
    assert (sends_on(db, DAY_D), sends_on(db, DAY_D1)) == (1, 1)  # nothing moved, nothing subtracted
    failed = state(db, first).outbound
    assert failed.status is OutboundStatus.FAILED and failed.sending_at == DAY_D


def test_cross_day_retry_with_capacity_moves_the_messages_single_ledger_row(db: Database) -> None:
    first = approved_reply(db, "p-1")
    transport = FakeEmailTransport().script(FakeBehavior.FAIL_BEFORE_SUBMIT, FakeBehavior.ACCEPT)
    send(dispatcher(db, transport, clock=FrozenClock(DAY_D), limits=one_slot()), first)
    assert (sends_on(db, DAY_D), sends_on(db, DAY_D1)) == (1, 0)

    retried = send(dispatcher(db, transport, clock=FrozenClock(DAY_D1), limits=one_slot()), first, "corr-retry")
    assert retried.outcome is DispatchOutcome.ACCEPTED and retried.attempt_no == 2
    # Stage 3 ledger semantics: one row per message, dated by its latest sending_at. The
    # confirmed-not-accepted day-D attempt stops counting on day D; the message counts on D+1.
    assert (sends_on(db, DAY_D), sends_on(db, DAY_D1)) == (0, 1)
    current = state(db, first)
    assert current.outbound.sending_at == DAY_D1
    [reservation] = current.reservations
    assert (reservation.state, reservation.policy_date) == (QuotaReservationState.CONSUMED, local_date(DAY_D, policy.TZ))
    assert [a.claimed_at for a in current.attempts] == [DAY_D, DAY_D1]  # attempt history keeps day D


# ---- 2. Reconciliation while the submission is in flight --------------------------------------


class Pause:
    """Holds a transport call inside ``submit`` until released by the test."""

    def __init__(self) -> None:
        self.reached, self.release = threading.Event(), threading.Event()
        self.request: TransportRequest | None = None

    def __call__(self, req: TransportRequest) -> None:
        self.request = req
        self.reached.set()
        assert self.release.wait(10)


def start_worker(db_path: Path, outbound_id: str, transport: FakeEmailTransport, pause: Pause) -> tuple[threading.Thread, dict[str, object]]:
    results: dict[str, object] = {}

    def run() -> None:
        with Database(db_path, busy_timeout_ms=10_000) as db:
            try:
                results["a"] = send(dispatcher(db, transport), outbound_id, "corr-a")
            except BaseException as exc:  # noqa: BLE001 - asserted by the test
                results["a"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    assert pause.reached.wait(10)
    return thread, results


def finish(thread: threading.Thread, pause: Pause) -> None:
    pause.release.set()
    thread.join(10)
    assert not thread.is_alive()


def test_temporary_absence_during_submission_neither_resolves_nor_permits_retry(db_path: Path) -> None:
    with Database(db_path) as db:
        outbound_id = approved_reply(db)
    pause = Pause()
    transport = FakeEmailTransport().script(FakeStep(FakeBehavior.ACCEPT, before=pause), FakeBehavior.ACCEPT)
    reconciler = FakeReconciler(transport)
    thread, results = start_worker(db_path, outbound_id, transport, pause)
    with Database(db_path) as db:
        absent = dispatcher(db, transport, reconciler=reconciler).reconcile(request(outbound_id, "corr-b"))
        assert (absent.outcome, absent.outbound_status) == (DispatchOutcome.UNKNOWN, OutboundStatus.SENDING)
        assert absent.reason_codes == ("RECONCILIATION_NOT_FOUND",)
        retry = send(dispatcher(db, transport), outbound_id, "corr-c")
        assert retry.reason_codes == (DispatchCode.ATTEMPT_UNRESOLVED,) and not retry.transport_called
    finish(thread, pause)

    a = results["a"]
    assert isinstance(a, DispatchResult) and a.outcome is DispatchOutcome.ACCEPTED
    assert len(transport.calls) == 1
    with Database(db_path) as db:
        current = state(db, outbound_id)
        assert current.outbound.status is OutboundStatus.SENT and current.outbound.provider_message_id == "fake-1"
        assert [(x.state, x.provider_message_id) for x in current.attempts] == [(DispatchAttemptState.ACCEPTED, "fake-1")]
        # A later negative report cannot overwrite the confirmed acceptance.
        assert pause.request is not None
        transport.rejected[pause.request.rfc_message_id] = True
        later = dispatcher(db, transport, reconciler=reconciler).reconcile(request(outbound_id, "corr-d"))
        assert later.outcome is DispatchOutcome.ACCEPTED and later.replayed and len(reconciler.lookups) == 1
        service = dispatcher(db, transport)
        negative = TransportResult(outcome=TransportOutcome.NOT_ACCEPTED, reason_code="LATE_NEGATIVE", retryable=True)
        replay = service._record(current.attempts[0], negative, "corr-e", reconciled=True, transport_called=False)  # noqa: SLF001
        assert replay.outcome is DispatchOutcome.ACCEPTED
        assert state(db, outbound_id).attempts == current.attempts


def test_positive_reconciliation_and_normal_finalization_agree(db_path: Path) -> None:
    with Database(db_path) as db:
        outbound_id = approved_reply(db)
    pause = Pause()
    transport = FakeEmailTransport().script(FakeStep(FakeBehavior.ACCEPT, after=pause))  # accepted, response held
    thread, results = start_worker(db_path, outbound_id, transport, pause)
    with Database(db_path) as db:
        reconciled = dispatcher(db, transport, reconciler=FakeReconciler(transport)).reconcile(request(outbound_id, "corr-b"))
        assert (reconciled.outcome, reconciled.outbound_status) == (DispatchOutcome.ACCEPTED, OutboundStatus.SENT)
    finish(thread, pause)
    a = results["a"]
    assert isinstance(a, DispatchResult) and a.outcome is DispatchOutcome.ACCEPTED and a.replayed
    with Database(db_path) as db:
        current = state(db, outbound_id)
        [attempt] = current.attempts
        assert attempt.provider_message_id == current.outbound.provider_message_id == reconciled.provider_message_id
        with db.transaction() as uow:
            thread_row = uow.threads.get(current.outbound.thread_id or "")
            assert thread_row is not None
            ours = [m for m in (uow.messages.get(i) for i in thread_row.message_ids) if m and m.rfc_message_id == attempt.rfc_message_id]
        assert len(ours) == 1  # recorded once, not by both paths
    assert len(transport.calls) == 1


def test_positively_established_non_acceptance_permits_a_safe_retry(db: Database) -> None:
    outbound_id = approved_reply(db)

    class Crash(BaseException):
        pass

    def crash(_: TransportRequest) -> None:
        raise Crash()

    transport = FakeEmailTransport().script(FakeStep(FakeBehavior.REJECT, retryable=True, after=crash), FakeBehavior.ACCEPT)
    try:
        send(dispatcher(db, transport), outbound_id)
    except Crash:
        pass
    assert state(db, outbound_id).attempts[0].state is DispatchAttemptState.CLAIMED
    resolved = dispatcher(db, transport, reconciler=FakeReconciler(transport)).reconcile(request(outbound_id, "corr-rec"))
    assert (resolved.outcome, resolved.outbound_status, resolved.reason_codes) == (
        DispatchOutcome.NOT_ACCEPTED, OutboundStatus.FAILED, ("RECONCILED_REJECTED",),
    )
    retried = send(dispatcher(db, transport), outbound_id, "corr-retry")
    assert retried.outcome is DispatchOutcome.ACCEPTED and retried.attempt_no == 2 and len(transport.calls) == 2


def wrongly_reported_rejection(db_path: Path, *, retry_first: bool) -> tuple[str, FakeEmailTransport, dict[str, object]]:
    """Attempt 1 is in flight; the provider (wrongly) reports a terminal rejection of it;
    optionally a retry runs; then attempt 1's acceptance arrives late."""
    with Database(db_path) as db:
        outbound_id = approved_reply(db)
    pause = Pause()
    transport = FakeEmailTransport().script(FakeStep(FakeBehavior.ACCEPT, before=pause), FakeBehavior.ACCEPT)
    thread, results = start_worker(db_path, outbound_id, transport, pause)
    assert pause.request is not None
    transport.rejected[pause.request.rfc_message_id] = True  # positive (but wrong) terminal evidence
    with Database(db_path) as db:
        reconciled = dispatcher(db, transport, reconciler=FakeReconciler(transport)).reconcile(request(outbound_id, "corr-b"))
        assert reconciled.outcome is DispatchOutcome.NOT_ACCEPTED
        if retry_first:
            assert send(dispatcher(db, transport), outbound_id, "corr-retry").outcome is DispatchOutcome.ACCEPTED
    finish(thread, pause)
    return outbound_id, transport, results


def conflicts(db: Database, outbound_id: str) -> list[dict[str, object]]:
    with db.transaction() as uow:
        events = uow.audit.list_for_subject(EntityRef(kind=RefKind.OUTBOUND_MESSAGE, id=outbound_id))
    return [dict(e.after or {}) for e in events if e.event_type == "DISPATCH_RESULT_CONFLICT"]


def test_late_acceptance_after_a_newer_retry_is_recorded_not_merged(db_path: Path) -> None:
    outbound_id, transport, results = wrongly_reported_rejection(db_path, retry_first=True)
    a = results["a"]
    assert isinstance(a, DispatchResult) and a.reason_codes == (DispatchCode.LATE_RESULT_CONFLICT,)
    assert len(transport.calls) == 2  # the wrong terminal evidence allowed the retry; both are recorded honestly
    with Database(db_path) as db:
        current = state(db, outbound_id)
        assert [x.state for x in current.attempts] == [DispatchAttemptState.NOT_ACCEPTED, DispatchAttemptState.ACCEPTED]
        assert current.outbound.status is OutboundStatus.SENT
        assert current.outbound.provider_message_id == current.attempts[1].provider_message_id
        [conflict] = conflicts(db, outbound_id)
        assert conflict["late_outcome"] == "ACCEPTED" and conflict["corrected_to_accepted"] is False
        assert conflict["late_provider_message_id"] == transport.accepted[current.attempts[0].rfc_message_id]


def test_late_acceptance_without_a_newer_attempt_corrects_the_record_and_stops_retries(db_path: Path) -> None:
    outbound_id, transport, results = wrongly_reported_rejection(db_path, retry_first=False)
    a = results["a"]
    assert isinstance(a, DispatchResult) and (a.outcome, a.outbound_status) == (DispatchOutcome.ACCEPTED, OutboundStatus.SENT)
    with Database(db_path) as db:
        current = state(db, outbound_id)
        [attempt] = current.attempts
        assert attempt.state is DispatchAttemptState.ACCEPTED and attempt.reason_code == DispatchCode.LATE_RESULT_CONFLICT
        assert current.outbound.failure_reason is None and current.outbound.provider_message_id == attempt.provider_message_id
        assert conflicts(db, outbound_id)[0]["corrected_to_accepted"] is True
        later = send(dispatcher(db, transport), outbound_id, "corr-later")
        assert later.replayed and not later.transport_called
    assert len(transport.calls) == 1


def test_late_acceptance_evidence_blocks_any_further_attempt_even_after_restart(db_path: Path) -> None:
    with Database(db_path) as db:
        outbound_id = approved_reply(db)
    first_pause, second_pause = Pause(), Pause()
    transport = FakeEmailTransport().script(
        FakeStep(FakeBehavior.ACCEPT, before=first_pause),  # attempt 1: accepted, but only reported late
        FakeStep(FakeBehavior.REJECT, retryable=True, before=second_pause),  # attempt 2
        FakeBehavior.ACCEPT,  # attempt 3 must never happen
    )
    # 1. Attempt 1 is in flight; wrong terminal evidence records it as retryable NOT_ACCEPTED.
    first, first_results = start_worker(db_path, outbound_id, transport, first_pause)
    assert first_pause.request is not None
    transport.rejected[first_pause.request.rfc_message_id] = True
    with Database(db_path) as db:
        assert dispatcher(db, transport, reconciler=FakeReconciler(transport)).reconcile(
            request(outbound_id, "corr-rec")).outcome is DispatchOutcome.NOT_ACCEPTED
    # 2. Attempt 2 is claimed and stays unresolved (paused inside submit).
    second, second_results = start_worker(db_path, outbound_id, transport, second_pause)
    # 3. Attempt 1's positive acceptance arrives late.
    finish(first, first_pause)
    late = first_results["a"]
    assert isinstance(late, DispatchResult) and late.reason_codes == (DispatchCode.LATE_RESULT_CONFLICT,)
    # 4. Attempt 2 becomes retryable NOT_ACCEPTED.
    finish(second, second_pause)
    assert isinstance(second_results["a"], DispatchResult) and second_results["a"].outcome is DispatchOutcome.NOT_ACCEPTED

    # 5. Further dispatch requests, in this process and after a restart, never submit again.
    with Database(db_path) as db:
        blocked = send(dispatcher(db, transport), outbound_id, "corr-again")
        assert blocked.outcome is DispatchOutcome.BLOCKED
        assert blocked.reason_codes == (DispatchCode.ACCEPTANCE_EVIDENCE_CONFLICT,)
    with Database(db_path) as restarted_db:
        restarted = dispatcher(restarted_db, transport)
        assert send(restarted, outbound_id, "corr-after-restart").reason_codes == (DispatchCode.ACCEPTANCE_EVIDENCE_CONFLICT,)
        view = restarted.inspect(outbound_id)
        current = state(restarted_db, outbound_id)
    assert len(transport.calls) == 2

    # Both attempts keep their recorded history; the late provider reference is durable.
    assert [a.state for a in current.attempts] == [DispatchAttemptState.NOT_ACCEPTED, DispatchAttemptState.NOT_ACCEPTED]
    late_ref = transport.accepted[first_pause.request.rfc_message_id]
    assert current.attempts[0].late_acceptance_provider_message_id == late_ref
    assert current.attempts[1].late_acceptance_provider_message_id is None
    assert current.outbound.status is OutboundStatus.FAILED  # message history is not rewritten
    # Typed read: the conflict is visible without reading raw audit events.
    assert view.acceptance_conflict and view.outbound_status is OutboundStatus.FAILED
    assert [(a.attempt_no, a.late_acceptance_provider_message_id) for a in view.attempts] == [(1, late_ref), (2, None)]
