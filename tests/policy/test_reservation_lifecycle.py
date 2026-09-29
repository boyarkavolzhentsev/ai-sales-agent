"""Stale ACTIVE reservations never strand an outbound message.

Yesterday = 2025-12-31 in Europe/Kyiv; today = 2026-01-01 (T0).
"""

import threading
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from app.core.enums import OutboundKind
from app.core.models import OutboundMessage
from app.persistence import AlreadyExistsError, Database, FrozenClock
from app.persistence.records import QuotaReservation, QuotaReservationState
from app.policy import (
    DuplicateQuotaReservationError,
    LimitPolicy,
    PolicyReason,
    QuotaExceededError,
    reserve_quota,
)
from tests.persistence import factories as f
from tests.policy import builders as b
from tests.policy.conftest import seed

YESTERDAY = b.T0 - timedelta(days=1)
TODAY = date(2026, 1, 1)


def add_messages(db: Database, *messages: OutboundMessage) -> None:
    with db.transaction() as uow:
        for message in messages:
            uow.outbound.add(message)


def reserve(
    db: Database,
    outbound_id: str,
    reservation_id: str,
    *,
    limits: LimitPolicy | None = None,
    now: datetime = b.T0,
) -> QuotaReservation:
    with db.transaction() as uow:
        message = uow.outbound.get(outbound_id)
        assert message is not None
        return reserve_quota(uow, limits or b.limits(sends=1), message, reservation_id=reservation_id, now=now)


def get(db: Database, reservation_id: str) -> QuotaReservation:
    with db.transaction() as uow:
        reservation = uow.quota_reservations.get(reservation_id)
    assert reservation is not None
    return reservation


def exists(db: Database, reservation_id: str) -> bool:
    with db.transaction() as uow:
        return uow.quota_reservations.get(reservation_id) is not None


def live(db: Database, outbound_id: str) -> QuotaReservation | None:
    with db.transaction() as uow:
        return uow.quota_reservations.get_live_for_outbound(outbound_id)


def consume(db: Database, reservation: QuotaReservation) -> QuotaReservation:
    consumed = reservation.model_copy(update={"state": QuotaReservationState.CONSUMED, "version": reservation.version + 1})
    with db.transaction() as uow:
        uow.quota_reservations.update(consumed, expected_version=reservation.version)
    return consumed


@pytest.fixture
def stale(seeded: Database) -> Database:
    """Message "a" holds an ACTIVE reservation from yesterday (it crashed before dispatch)."""
    add_messages(
        seeded,
        b.approved_message("a", created_at=YESTERDAY, approved_at=YESTERDAY),
        b.approved_message("b"),
    )
    old = reserve(seeded, "a", "r-old", now=YESTERDAY)
    assert (old.state, old.policy_date) == (QuotaReservationState.ACTIVE, date(2025, 12, 31))
    return seeded


# A
def test_yesterdays_active_reservation_does_not_count_today(stale: Database) -> None:
    # Limit 1/day: "b" can take today's only slot although "a" still holds yesterday's.
    assert reserve(stale, "b", "r-b").policy_date == TODAY
    assert get(stale, "r-old").state is QuotaReservationState.ACTIVE


# B, C, D
def test_same_outbound_can_reserve_again_today_and_old_one_is_released(stale: Database) -> None:
    new = reserve(stale, "a", "r-new")
    # D: the new reservation is ACTIVE for today
    assert (new.state, new.policy_date, new.outbound_id) == (QuotaReservationState.ACTIVE, TODAY, "a")
    assert live(stale, "a") == new
    # C: the stale one is RELEASED through a normal versioned update
    old = get(stale, "r-old")
    assert (old.state, old.version, old.updated_at) == (QuotaReservationState.RELEASED, 2, b.T0)
    assert old.policy_date == date(2025, 12, 31)


# E
def test_same_day_active_reservation_rejects_duplicate(seeded: Database) -> None:
    add_messages(seeded, b.approved_message("a"))
    first = reserve(seeded, "a", "r-1")
    later_today = b.T0 + timedelta(hours=3)
    with pytest.raises(DuplicateQuotaReservationError) as info:
        reserve(seeded, "a", "r-2", now=later_today)
    assert info.value.existing == first
    assert not exists(seeded, "r-2")
    assert live(seeded, "a") == first


# F
@pytest.mark.parametrize("when", [b.T0, YESTERDAY + timedelta(hours=1)], ids=["next-day", "same-day"])
def test_consumed_reservation_always_rejects_another(stale: Database, when: datetime) -> None:
    consumed = consume(stale, get(stale, "r-old"))
    with pytest.raises(DuplicateQuotaReservationError) as info:
        reserve(stale, "a", "r-new", now=when)
    assert info.value.existing == consumed
    assert not exists(stale, "r-new")
    assert get(stale, "r-old") == consumed


# G: all-or-nothing
def test_exhausted_today_after_stale_release_rolls_back_everything(stale: Database) -> None:
    reserve(stale, "b", "r-b")  # takes today's only slot
    with pytest.raises(QuotaExceededError) as info:
        reserve(stale, "a", "r-new")
    assert [c.reason for c in info.value.checks] == [PolicyReason.GLOBAL_DAILY_LIMIT]
    assert not exists(stale, "r-new")
    unchanged = get(stale, "r-old")
    assert (unchanged.state, unchanged.version) == (QuotaReservationState.ACTIVE, 1)


def test_all_or_nothing_even_when_caller_catches_and_commits(stale: Database) -> None:
    reserve(stale, "b", "r-b")
    with stale.transaction() as uow:
        message = uow.outbound.get("a")
        assert message is not None
        with pytest.raises(QuotaExceededError):
            reserve_quota(uow, b.limits(sends=1), message, reservation_id="r-new", now=b.T0)
        uow.audit.append(  # the caller records the HOLD and commits its transaction
            f.audit_event(event_id="evt-hold", event_type="OUTBOUND_HELD")
        )
    assert get(stale, "r-old").state is QuotaReservationState.ACTIVE
    assert not exists(stale, "r-new")
    with stale.transaction() as uow:
        assert uow.audit.get("evt-hold") is not None


def test_reusing_the_stale_reservation_id_is_rejected_atomically(stale: Database) -> None:
    with pytest.raises(AlreadyExistsError):
        reserve(stale, "a", "r-old")
    assert get(stale, "r-old").state is QuotaReservationState.ACTIVE


def test_counts_are_taken_after_the_stale_release(seeded: Database) -> None:
    # An ACTIVE follow-up reservation counts against the contact's all-time follow-up cap
    # whatever its date. With a cap of 1, re-reserving succeeds only if the stale
    # reservation is released before quota is recounted.
    follow_up = b.approved_message(
        "fu", kind=OutboundKind.FOLLOW_UP, sequence_no=1, thread_id=f.THREAD_ID,
        created_at=YESTERDAY, approved_at=YESTERDAY,
    )
    add_messages(seeded, follow_up)
    capped = b.limits(sends=5, per_contact=1)
    reserve(seeded, "fu", "r-old", limits=capped, now=YESTERDAY)
    new = reserve(seeded, "fu", "r-new", limits=capped)
    assert (new.state, new.policy_date) == (QuotaReservationState.ACTIVE, TODAY)
    assert get(seeded, "r-old").state is QuotaReservationState.RELEASED


# H: races
@pytest.fixture
def stale_file_db(db_path: Path, clock: FrozenClock) -> Path:
    with Database(db_path) as db:
        db.initialize_schema(clock)
        seed(db)
        add_messages(db, b.approved_message("a", created_at=YESTERDAY, approved_at=YESTERDAY), b.approved_message("b"))
        reserve(db, "a", "r-old", now=YESTERDAY)
    return db_path


def _race(path: Path, jobs: dict[str, tuple[str, str]]) -> dict[str, str]:
    barrier = threading.Barrier(len(jobs))
    outcomes: dict[str, str] = {}

    def worker(name: str, outbound_id: str, reservation_id: str) -> None:
        with Database(path, busy_timeout_ms=10_000) as db:
            barrier.wait()
            try:
                reserve(db, outbound_id, reservation_id)
                outcomes[name] = "reserved"
            except QuotaExceededError:
                outcomes[name] = "quota_exceeded"
            except DuplicateQuotaReservationError:
                outcomes[name] = "duplicate"

    threads = [threading.Thread(target=worker, args=(name, *job)) for name, job in jobs.items()]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    return outcomes


def test_race_to_re_reserve_the_same_stale_message(stale_file_db: Path) -> None:
    outcomes = _race(stale_file_db, {"w1": ("a", "r-w1"), "w2": ("a", "r-w2")})
    assert sorted(outcomes.values()) == ["duplicate", "reserved"]
    with Database(stale_file_db) as db:
        current = live(db, "a")
        assert current is not None and current.policy_date == TODAY
        assert get(db, "r-old").state is QuotaReservationState.RELEASED


def test_race_for_last_slot_between_stale_and_fresh_message(stale_file_db: Path) -> None:
    outcomes = _race(stale_file_db, {"stale": ("a", "r-a"), "fresh": ("b", "r-b")})
    assert sorted(outcomes.values()) == ["quota_exceeded", "reserved"]
    with Database(stale_file_db) as db, db.transaction() as uow:
        assert len(uow.quota_reservations.list_active_for_date(TODAY)) == 1
        old = uow.quota_reservations.get("r-old")
        assert old is not None
        # The stale reservation is released only if the stale message won the slot.
        expected = QuotaReservationState.RELEASED if outcomes["stale"] == "reserved" else QuotaReservationState.ACTIVE
        assert old.state is expected
