"""Releasing quota reservations. Pure persistence of the Stage 3 lifecycle, no counting.

ACTIVE -> RELEASED is the only transition here. A CONSUMED reservation belongs to a
dispatched message that the send ledger already counts; it is never released. Releasing
never creates reservations or permits. Callers run it inside their own transaction, so
the release is atomic with whatever made the reservation obsolete.
"""

from datetime import datetime

from app.persistence import UnitOfWork
from app.persistence.records import QuotaReservation, QuotaReservationState


def release_reservation(uow: UnitOfWork, reservation: QuotaReservation, now: datetime) -> QuotaReservation:
    """Move one ACTIVE reservation to RELEASED (versioned update)."""
    if reservation.state is not QuotaReservationState.ACTIVE:
        raise ValueError(f"only ACTIVE reservations can be released (got {reservation.state})")
    released = QuotaReservation.model_validate(
        reservation.model_dump()
        | {
            "state": QuotaReservationState.RELEASED,
            "updated_at": max(now, reservation.updated_at),
            "version": reservation.version + 1,
        }
    )
    uow.quota_reservations.update(released, expected_version=reservation.version)
    return released


def release_for_cancelled_message(uow: UnitOfWork, outbound_id: str, now: datetime) -> QuotaReservation | None:
    """Free the slot held for a message that will never be dispatched.

    Returns the released reservation, or None when there is nothing to release (no live
    reservation, or a CONSUMED one, which is preserved). Idempotent: a second call finds
    no ACTIVE reservation.
    """
    live = uow.quota_reservations.get_live_for_outbound(outbound_id)
    if live is None or live.state is not QuotaReservationState.ACTIVE:
        return None
    return release_reservation(uow, live, now)
