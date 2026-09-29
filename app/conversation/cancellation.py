"""Cancelling not-yet-dispatched outbound messages, shared by inbound handling (Stage 6),
conversation state changes and operator actions, so every path releases quota the same way."""

from datetime import datetime

from app.core.enums import OutboundStatus
from app.core.models import OutboundMessage
from app.persistence import UnitOfWork
from app.policy.release import release_for_cancelled_message

# Messages that could still become (or already are) eligible for dispatch.
UNDISPATCHED_STATUSES = frozenset(
    {
        OutboundStatus.DRAFTED,
        OutboundStatus.PENDING_REVIEW,
        OutboundStatus.HELD,
        OutboundStatus.OPERATOR_APPROVED,
        OutboundStatus.APPROVED,
    }
)


def cancel_undispatched(
    uow: UnitOfWork, messages: list[OutboundMessage], now: datetime
) -> tuple[list[OutboundMessage], list[str]]:
    """Cancel every given message that is not yet dispatched and release any ACTIVE quota
    reservation it held, in the caller's transaction. SENDING and later (dispatched
    history) and CONSUMED reservations are never touched. Returns (cancelled, released
    reservation ids)."""
    cancelled: list[OutboundMessage] = []
    released: list[str] = []
    for message in messages:
        if message.status not in UNDISPATCHED_STATUSES:
            continue
        uow.outbound.update(
            OutboundMessage.model_validate(
                message.model_dump() | {"status": OutboundStatus.CANCELLED, "version": message.version + 1}
            ),
            message.version,
        )
        cancelled.append(message)
        reservation = release_for_cancelled_message(uow, message.outbound_id, now)
        if reservation is not None:
            released.append(reservation.reservation_id)
    return cancelled, released
