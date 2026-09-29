"""Cancelling an undispatched message releases its ACTIVE quota reservation atomically.

Messages reach APPROVED here the way a future send gate would move them (permit id and
approved_at set); reservations are made with the real Stage 3 ``reserve_quota``.
"""

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest

from app.core.enums import DNCScope, LeadIntent, OutboundDecision, OutboundStatus, RefKind
from app.core.models import EntityRef, OutboundMessage
from app.inbound import InboundProcessingError
from app.conversation import cancellation as cancellation_module
from app.llm import LLMTask
from app.persistence import Database, QuotaReservation, QuotaReservationState, UnitOfWork
from app.policy import QuotaExceededError, release_for_cancelled_message, release_reservation, reserve_quota
from tests.inbound.builders import NOW, SENDER, ScriptedTransport, classification, envelope, happy_transport, process
from tests.policy import builders as policy

OTHER = "other@elsewhere.example"
ONE_SLOT = policy.limits(sends=1, new_contacts=10)


@pytest.fixture
def db(db_path: Path) -> Iterator[Database]:
    with Database(db_path) as database:
        yield database


def approved(db: Database, provider_message_id: str, sender: str = SENDER) -> OutboundMessage:
    result = process(db, happy_transport(), envelope(provider_message_id, sender=sender))
    with db.transaction() as uow:
        drafted = uow.outbound.get(result.outbound_id or "")
        assert drafted is not None
        message = OutboundMessage.model_validate(
            drafted.model_dump()
            | {"status": OutboundStatus.APPROVED, "decision": OutboundDecision.SEND, "send_permit_id": f"permit-{provider_message_id}",
               "approved_at": NOW, "version": drafted.version + 1}
        )
        uow.outbound.update(message, drafted.version)
    return message


def reserve(db: Database, message: OutboundMessage, reservation_id: str) -> QuotaReservation:
    with db.transaction() as uow:
        return reserve_quota(uow, ONE_SLOT, message, reservation_id=reservation_id, now=NOW)


def reservation(db: Database, reservation_id: str) -> QuotaReservation:
    with db.transaction() as uow:
        found = uow.quota_reservations.get(reservation_id)
    assert found is not None
    return found


def status(db: Database, outbound_id: str) -> OutboundStatus:
    with db.transaction() as uow:
        found = uow.outbound.get(outbound_id)
    assert found is not None
    return found.status


def unsubscribe(db: Database, provider_message_id: str = "p-unsub", sender: str = SENDER) -> None:
    process(db, ScriptedTransport().script(LLMTask.INTENT_CLASSIFICATION, classification(LeadIntent.UNSUBSCRIBE)),
            envelope(provider_message_id, sender=sender, body="Please unsubscribe me."))


def released_ids(db: Database, outbound_id: str) -> list[str]:
    with db.transaction() as uow:
        events = uow.audit.list_for_subject(EntityRef(kind=RefKind.OUTBOUND_MESSAGE, id=outbound_id))
    ids: list[str] = []
    for event in events:
        if event.event_type == "DRAFTS_CANCELLED" and event.after is not None:
            released = event.after["released_reservation_ids"]
            assert isinstance(released, list)
            ids += [str(i) for i in released]
    return ids


# ---- The capacity is actually freed ----------------------------------------------------------


def test_unsubscribe_cancels_the_approved_message_and_frees_its_slot(db: Database) -> None:
    first, second = approved(db, "p-1"), approved(db, "p-2", sender=OTHER)
    reserve(db, first, "r-1")
    with pytest.raises(QuotaExceededError):
        reserve(db, second, "r-2")  # the only slot is held by the first message

    unsubscribe(db)
    assert status(db, first.outbound_id) is OutboundStatus.CANCELLED
    freed = reservation(db, "r-1")
    assert (freed.state, freed.version) == (QuotaReservationState.RELEASED, 2)
    assert released_ids(db, first.outbound_id) == ["r-1"]
    assert reserve(db, second, "r-2").state is QuotaReservationState.ACTIVE  # capacity is back


def test_lead_closure_releases_the_slot_too(db: Database) -> None:
    first, second = approved(db, "p-1"), approved(db, "p-2", sender=OTHER)
    reserve(db, first, "r-1")
    process(db, ScriptedTransport().script(LLMTask.INTENT_CLASSIFICATION, classification(LeadIntent.NOT_INTERESTED)),
            envelope("p-no", body="Not interested, thanks.", in_reply_to="<p-1@prospect.example>"))
    assert status(db, first.outbound_id) is OutboundStatus.CANCELLED
    assert reservation(db, "r-1").state is QuotaReservationState.RELEASED
    reserve(db, second, "r-2")


def test_repeated_processing_never_releases_twice(db: Database) -> None:
    first, second = approved(db, "p-1"), approved(db, "p-2", sender=OTHER)
    reserve(db, first, "r-1")
    unsubscribe(db)
    unsubscribe(db)  # identical redelivery: replayed
    unsubscribe(db, "p-unsub-2")  # a second unsubscribe: nothing left to cancel or release
    freed = reservation(db, "r-1")
    assert (freed.state, freed.version) == (QuotaReservationState.RELEASED, 2)
    assert released_ids(db, first.outbound_id) == ["r-1"]
    reserve(db, second, "r-2")
    with db.transaction() as uow:
        assert [r.reservation_id for r in uow.quota_reservations.list_active_for_date(freed.policy_date)] == ["r-2"]
        assert release_for_cancelled_message(uow, first.outbound_id, NOW) is None  # idempotent


# ---- Atomicity ----------------------------------------------------------------------------------


def test_a_release_failure_rolls_back_cancellation_and_release(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    first = approved(db, "p-1")
    reserve(db, first, "r-1")

    def broken(*args: object) -> None:
        raise RuntimeError("reservation store unavailable")

    monkeypatch.setattr(cancellation_module, "release_for_cancelled_message", broken)
    with pytest.raises(InboundProcessingError):
        unsubscribe(db)
    assert status(db, first.outbound_id) is OutboundStatus.APPROVED  # not cancelled without its release
    assert reservation(db, "r-1").state is QuotaReservationState.ACTIVE
    with db.transaction() as uow:
        assert uow.dnc.list_active(DNCScope.EMAIL, SENDER, NOW) == []


def test_a_transient_failure_falls_back_without_losing_the_unsubscribe(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    first = approved(db, "p-1")
    reserve(db, first, "r-1")
    calls: list[str] = []
    real = release_for_cancelled_message

    def flaky(uow: UnitOfWork, outbound_id: str, now: datetime) -> QuotaReservation | None:
        calls.append(outbound_id)
        if len(calls) == 1:
            raise RuntimeError("transient")
        return real(uow, outbound_id, now)

    monkeypatch.setattr(cancellation_module, "release_for_cancelled_message", flaky)
    unsubscribe(db)
    assert status(db, first.outbound_id) is OutboundStatus.CANCELLED
    assert reservation(db, "r-1").state is QuotaReservationState.RELEASED
    with db.transaction() as uow:
        assert len(uow.dnc.list_active(DNCScope.EMAIL, SENDER, NOW)) == 1


# ---- Other states ------------------------------------------------------------------------------


def test_operator_approved_without_a_reservation_still_cancels(db: Database) -> None:
    result = process(db, happy_transport(), envelope("p-1"))
    with db.transaction() as uow:
        drafted = uow.outbound.get(result.outbound_id or "")
        assert drafted is not None
        uow.outbound.update(
            OutboundMessage.model_validate(drafted.model_dump() | {"status": OutboundStatus.OPERATOR_APPROVED, "decision": OutboundDecision.SEND,
                                                                  "approved_at": NOW, "version": 2}),
            1,
        )
    unsubscribe(db)
    assert status(db, result.outbound_id or "") is OutboundStatus.CANCELLED
    assert released_ids(db, result.outbound_id or "") == []


def test_dispatched_history_and_consumed_quota_are_untouched(db: Database) -> None:
    first = approved(db, "p-1")
    held = reserve(db, first, "r-1")
    with db.transaction() as uow:
        uow.quota_reservations.update(held.model_copy(update={"state": QuotaReservationState.CONSUMED, "version": 2}), 1)
        sent = first.model_copy(update={"status": OutboundStatus.SENT, "sending_at": NOW, "sent_at": NOW, "version": first.version + 1})
        uow.outbound.update(OutboundMessage.model_validate(sent.model_dump()), first.version)
    unsubscribe(db)
    assert status(db, first.outbound_id) is OutboundStatus.SENT
    consumed = reservation(db, "r-1")
    assert (consumed.state, consumed.version) == (QuotaReservationState.CONSUMED, 2)
    with db.transaction() as uow:
        assert release_for_cancelled_message(uow, first.outbound_id, NOW) is None
        with pytest.raises(ValueError, match="only ACTIVE"):
            release_reservation(uow, consumed, NOW)
