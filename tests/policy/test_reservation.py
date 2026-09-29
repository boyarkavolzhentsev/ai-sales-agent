"""Transaction-bound quota reservation against local SQLite."""

import threading
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.core.enums import OutboundKind, OutboundStatus
from app.core.models import OutboundMessage
from app.persistence import Database, FrozenClock, PersistenceError
from app.persistence.records import QuotaReservation, QuotaReservationState
from app.policy import (
    DuplicateQuotaReservationError,
    LimitPolicy,
    PolicyError,
    PolicyReason,
    QuotaExceededError,
    reserve_quota,
)
from tests.persistence import factories as f
from tests.policy import builders as b
from tests.policy.conftest import seed


def add_messages(db: Database, *messages: OutboundMessage) -> None:
    with db.transaction() as uow:
        for message in messages:
            uow.outbound.add(message)


def reserve(
    db: Database,
    outbound_id: str,
    reservation_id: str,
    limits: LimitPolicy | None = None,
    now: datetime = b.T0,
) -> QuotaReservation:
    with db.transaction() as uow:
        message = uow.outbound.get(outbound_id)
        assert message is not None
        return reserve_quota(uow, limits or b.limits(sends=2), message, reservation_id=reservation_id, now=now)


def active_count(db: Database) -> int:
    with db.transaction() as uow:
        return len(uow.quota_reservations.list_active_for_date(b.T0.date()))


# ---- I. Reservation ---------------------------------------------------------------------


def test_available_slot_succeeds(seeded: Database) -> None:
    add_messages(seeded, b.approved_message("a"))
    reservation = reserve(seeded, "a", "r-a")
    assert reservation.state is QuotaReservationState.ACTIVE
    assert (reservation.mailbox, reservation.campaign_id, reservation.kind) == (b.MAILBOX, f.CAMPAIGN_ID, OutboundKind.FIRST_TOUCH)
    with seeded.transaction() as uow:
        assert uow.quota_reservations.get("r-a") == reservation
        assert uow.quota_reservations.get_live_for_outbound("a") == reservation


def test_exhausted_limit_fails_counting_ledger_and_reservations(seeded: Database) -> None:
    add_messages(seeded, b.sent_message("sent-1"), b.approved_message("a"), b.approved_message("b"))
    reserve(seeded, "a", "r-a")  # ledger 1 + reservation 1 = 2 = limit
    with pytest.raises(QuotaExceededError) as info:
        reserve(seeded, "b", "r-b")
    assert [c.reason for c in info.value.checks] == [PolicyReason.GLOBAL_DAILY_LIMIT]
    assert active_count(seeded) == 1


def test_every_exhausted_limit_is_reported(seeded: Database) -> None:
    add_messages(seeded, b.sent_message("sent-1"), b.approved_message("a"))
    policy = b.limits(sends=1, new_contacts=1, mailboxes=(b.mailbox_limits(max_sends_per_day=1),))
    with pytest.raises(QuotaExceededError) as info:
        reserve(seeded, "a", "r-a", limits=policy)
    assert {c.reason for c in info.value.checks} == {
        PolicyReason.GLOBAL_DAILY_LIMIT,
        PolicyReason.MAILBOX_DAILY_LIMIT,
        PolicyReason.NEW_CONTACT_DAILY_LIMIT,
    }


def test_duplicate_reservation_does_not_take_a_second_slot(seeded: Database) -> None:
    add_messages(seeded, b.approved_message("a"))
    first = reserve(seeded, "a", "r-1")
    with pytest.raises(DuplicateQuotaReservationError) as info:
        reserve(seeded, "a", "r-2")
    assert info.value.existing == first
    assert active_count(seeded) == 1


def test_rollback_releases_the_reservation(seeded: Database) -> None:
    add_messages(seeded, b.approved_message("a"), b.approved_message("b"))
    policy = b.limits(sends=1)
    with pytest.raises(RuntimeError), seeded.transaction() as uow:
        message = uow.outbound.get("a")
        assert message is not None
        reserve_quota(uow, policy, message, reservation_id="r-a", now=b.T0)
        raise RuntimeError("send pipeline failed before commit")
    assert active_count(seeded) == 0
    assert reserve(seeded, "b", "r-b", limits=policy).outbound_id == "b"


def test_released_slot_can_be_reserved_again(seeded: Database) -> None:
    add_messages(seeded, b.approved_message("a"), b.approved_message("b"))
    policy = b.limits(sends=1)
    held = reserve(seeded, "a", "r-a", limits=policy)
    with pytest.raises(QuotaExceededError):
        reserve(seeded, "b", "r-b", limits=policy)
    with seeded.transaction() as uow:
        released = held.model_copy(update={"state": QuotaReservationState.RELEASED, "version": 2})
        uow.quota_reservations.update(released, expected_version=1)
    assert reserve(seeded, "b", "r-b", limits=policy).state is QuotaReservationState.ACTIVE
    assert reserve(seeded, "a", "r-a2", limits=b.limits(sends=2)).reservation_id == "r-a2"


def test_consumed_reservation_is_not_double_counted(seeded: Database) -> None:
    add_messages(seeded, b.approved_message("a"), b.approved_message("b"))
    held = reserve(seeded, "a", "r-a")
    with seeded.transaction() as uow:
        message = uow.outbound.get("a")
        assert message is not None
        dispatched = message.model_copy(
            update={"status": OutboundStatus.SENDING, "sending_at": b.T0, "version": message.version + 1}
        )
        uow.outbound.update(dispatched, expected_version=message.version)
        consumed = held.model_copy(update={"state": QuotaReservationState.CONSUMED, "version": 2})
        uow.quota_reservations.update(consumed, expected_version=1)
    # Limit 2: "a" is now in the ledger once, so exactly one slot remains.
    assert reserve(seeded, "b", "r-b").outbound_id == "b"


def test_reply_reservation_uses_thread_mailbox(seeded: Database) -> None:
    reply = b.approved_message("reply", kind=OutboundKind.REPLY, campaign_id=None, thread_id=f.THREAD_ID)
    add_messages(seeded, reply)
    policy = b.limits(mailboxes=(b.mailbox_limits(mailbox="support@ourco.example", max_sends_per_day=0),))
    with pytest.raises(QuotaExceededError) as info:
        reserve(seeded, "reply", "r-reply", limits=policy)
    assert [c.reason for c in info.value.checks] == [PolicyReason.MAILBOX_DAILY_LIMIT]
    assert reserve(seeded, "reply", "r-reply").mailbox == "support@ourco.example"


def test_only_approved_messages_can_reserve(seeded: Database) -> None:
    add_messages(seeded, f.outbound_message(outbound_id="draft", idempotency_key="k-draft"))
    with pytest.raises(PolicyError, match="APPROVED"):
        reserve(seeded, "draft", "r-draft")


def test_follow_up_reservation_respects_contact_cap(seeded: Database) -> None:
    earlier = [
        b.sent_message(
            f"fu{i}", kind=OutboundKind.FOLLOW_UP, sequence_no=i, thread_id=f.THREAD_ID,
            sending_at=b.T0 - timedelta(days=10 - i),
        )
        for i in (1, 2)
    ]
    add_messages(seeded, *earlier, b.approved_message("fu3", kind=OutboundKind.FOLLOW_UP, sequence_no=3, thread_id=f.THREAD_ID))
    with pytest.raises(QuotaExceededError) as info:
        reserve(seeded, "fu3", "r-fu3", limits=b.limits(per_contact=2))
    assert [c.reason for c in info.value.checks] == [PolicyReason.CONTACT_FOLLOWUP_LIMIT]


def test_day_boundary_uses_policy_timezone(seeded: Database) -> None:
    # Sent at 21:30 UTC on Jan 1 = 23:30 Kyiv on Jan 1. At 22:30 UTC it is Jan 2 in Kyiv.
    late = b.T0.replace(hour=21, minute=30)
    add_messages(seeded, b.sent_message("late", sending_at=late), b.approved_message("a", approved_at=late))
    with pytest.raises(QuotaExceededError):
        reserve(seeded, "a", "r-a", limits=b.limits(sends=1), now=late + timedelta(minutes=10))
    next_day = reserve(seeded, "a", "r-a", limits=b.limits(sends=1), now=late + timedelta(hours=1))
    assert next_day.policy_date.isoformat() == "2026-01-02"


# ---- K. Last-slot race -------------------------------------------------------------------


@pytest.fixture
def file_db(db_path: Path, clock: FrozenClock) -> Path:
    with Database(db_path) as db:
        db.initialize_schema(clock)
        seed(db)
        add_messages(db, b.approved_message("a"), b.approved_message("b"))
    return db_path


def test_last_slot_sequential_transactions(file_db: Path) -> None:
    policy = b.limits(sends=1)
    with Database(file_db) as first, Database(file_db) as second:
        assert reserve(first, "a", "r-a", limits=policy).outbound_id == "a"
        with pytest.raises(QuotaExceededError):
            reserve(second, "b", "r-b", limits=policy)
        assert active_count(second) == 1


def test_open_reservation_transaction_blocks_a_competing_writer(file_db: Path) -> None:
    policy = b.limits(sends=1)
    with Database(file_db) as first, Database(file_db, busy_timeout_ms=0) as second:
        with first.transaction() as uow:
            message = uow.outbound.get("a")
            assert message is not None
            reserve_quota(uow, policy, message, reservation_id="r-a", now=b.T0)
            # The second worker cannot even start its read-count-insert while this is open.
            with pytest.raises(PersistenceError, match="locked"):
                reserve(second, "b", "r-b", limits=policy)
        with pytest.raises(QuotaExceededError):
            reserve(second, "b", "r-b", limits=policy)
        assert active_count(second) == 1


def test_last_slot_concurrent_threads(file_db: Path) -> None:
    policy = b.limits(sends=1)
    barrier = threading.Barrier(2)
    outcomes: dict[str, str] = {}

    def worker(outbound_id: str) -> None:
        with Database(file_db, busy_timeout_ms=10_000) as db:  # each thread owns its connection
            barrier.wait()
            try:
                reserve(db, outbound_id, f"r-{outbound_id}", limits=policy)
                outcomes[outbound_id] = "reserved"
            except QuotaExceededError:
                outcomes[outbound_id] = "quota_exceeded"

    threads = [threading.Thread(target=worker, args=(oid,)) for oid in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert sorted(outcomes.values()) == ["quota_exceeded", "reserved"]
    with Database(file_db) as db:
        assert active_count(db) == 1
