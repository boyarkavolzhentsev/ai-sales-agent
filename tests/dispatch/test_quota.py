"""G. Reservation and ledger consistency under dispatch."""

from app.core.enums import LeadIntent, OutboundStatus
from app.dispatch import DispatchOutcome, FakeBehavior, FakeEmailTransport
from app.llm import LLMTask
from app.persistence import Database, QuotaReservationState
from app.policy import COUNTED_STATUSES, PolicyReason, QuotaSnapshot, build_quota_snapshot
from app.policy.windows import local_date, local_day_bounds_utc
from tests.dispatch.builders import approved_reply, dispatcher, one_slot, send, state
from tests.inbound.builders import MAILBOX, NOW, ScriptedTransport, classification, envelope, process
from tests.policy import builders as policy

OTHER = "other@elsewhere.example"


def usage(db: Database, contact_id: str) -> QuotaSnapshot:
    tz = policy.TZ
    start, end = local_day_bounds_utc(local_date(NOW, tz), tz)
    with db.transaction() as uow:
        entries = uow.outbound.list_ledger_entries(COUNTED_STATUSES, start, end)
        reservations = uow.quota_reservations.list_active_for_date(local_date(NOW, tz))
    return build_quota_snapshot(entries, reservations, now=NOW, timezone=tz, mailbox=MAILBOX, campaign_id=None, contact_id=contact_id)


def test_accepted_message_is_counted_exactly_once(db: Database) -> None:
    outbound_id = approved_reply(db)
    send(dispatcher(db), outbound_id)
    send(dispatcher(db), outbound_id, "corr-replay")
    current = state(db, outbound_id)
    [reservation] = current.reservations
    assert reservation.state is QuotaReservationState.CONSUMED and reservation.version == 2
    assert usage(db, current.outbound.contact_id).sends_today == 1  # ledger only; consumed slot not added


def test_unknown_outcome_conservatively_keeps_its_slot(db: Database) -> None:
    first = approved_reply(db, "p-1")
    second = approved_reply(db, "p-2", sender=OTHER)
    unknown = send(dispatcher(db, FakeEmailTransport().script(FakeBehavior.TIMEOUT), limits=one_slot()), first)
    assert unknown.outcome is DispatchOutcome.UNKNOWN
    assert usage(db, state(db, first).outbound.contact_id).sends_today == 1
    blocked = send(dispatcher(db, limits=one_slot()), second)
    assert blocked.outcome is DispatchOutcome.BLOCKED and blocked.reason_codes == (PolicyReason.GLOBAL_DAILY_LIMIT.value,)


def test_confirmed_non_acceptance_keeps_the_stage3_conservative_count(db: Database) -> None:
    outbound_id = approved_reply(db)
    send(dispatcher(db, FakeEmailTransport().script(FakeBehavior.FAIL_BEFORE_SUBMIT)), outbound_id)
    assert state(db, outbound_id).outbound.status is OutboundStatus.FAILED
    # Stage 3 counts FAILED (unchanged); the retry moves the same message back to SENDING.
    assert usage(db, state(db, outbound_id).outbound.contact_id).sends_today == 1
    send(dispatcher(db), outbound_id, "corr-retry")
    assert usage(db, state(db, outbound_id).outbound.contact_id).sends_today == 1


def test_retry_is_not_blocked_by_its_own_earlier_attempt(db: Database) -> None:
    outbound_id = approved_reply(db)
    send(dispatcher(db, FakeEmailTransport().script(FakeBehavior.FAIL_BEFORE_SUBMIT), limits=one_slot()), outbound_id)
    assert send(dispatcher(db, limits=one_slot()), outbound_id, "corr-retry").outcome is DispatchOutcome.ACCEPTED


def test_cancellation_before_claim_leaves_no_reservation_and_frees_nothing_twice(db: Database) -> None:
    first = approved_reply(db, "p-1")
    second = approved_reply(db, "p-2", sender=OTHER)
    process(db, ScriptedTransport().script(LLMTask.INTENT_CLASSIFICATION, classification(LeadIntent.UNSUBSCRIBE)),
            envelope("p-unsub", body="Please unsubscribe me."))
    assert state(db, first).outbound.status is OutboundStatus.CANCELLED and state(db, first).reservations == []
    assert send(dispatcher(db, limits=one_slot()), second).outcome is DispatchOutcome.ACCEPTED
