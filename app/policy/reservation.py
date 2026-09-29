"""Transaction-bound, atomic quota reservation.

Atomicity across writers: ``Database.transaction()`` opens ``BEGIN IMMEDIATE``, which
takes SQLite's write lock before the first read. Release-recount-insert therefore cannot
interleave with another writer on any connection or process: a competing reservation
either committed before this transaction began (and is seen) or waits until this one
finishes. Atomicity within the caller's transaction: a savepoint (see reserve_quota).

Staleness uses only the policy-date boundary; there are no wall-clock leases.
"""

from datetime import date, datetime

from app.core.enums import OutboundKind, OutboundStatus
from app.core.models import Campaign, OutboundMessage
from app.persistence import AlreadyExistsError, UnitOfWork
from app.persistence.records import LedgerEntry, QuotaReservation, QuotaReservationState
from app.policy.errors import DuplicateQuotaReservationError, PolicyError, QuotaExceededError
from app.policy.limits import LimitPolicy
from app.policy.quota import COUNTED_STATUSES, build_quota_snapshot, evaluate_quota
from app.policy.release import release_reservation
from app.policy.windows import local_date, local_day_bounds_utc


def reserve_quota(
    uow: UnitOfWork,
    limits: LimitPolicy,
    outbound: OutboundMessage,
    *,
    reservation_id: str,
    now: datetime,
) -> QuotaReservation:
    """Reserve one quota slot for an APPROVED outbound message, or raise.

    Lifecycle of the message's existing live reservation, if any:
    - CONSUMED: the message was dispatched; raise DuplicateQuotaReservationError.
    - ACTIVE for the current (or a later) policy date: raise DuplicateQuotaReservationError.
    - ACTIVE for an earlier policy date: stale (e.g. a crash before dispatch). It is
      moved to RELEASED, then quota is recounted for today and a new reservation made.

    All-or-nothing: the stale release and the new reservation happen inside one
    savepoint, so if the new reservation cannot be made (QuotaExceededError or any other
    error) the stale release is undone too, even if the caller catches the error and
    commits the surrounding transaction. Sends nothing.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if outbound.status is not OutboundStatus.APPROVED:
        raise PolicyError(f"only APPROVED messages can reserve quota (got {outbound.status})")

    policy_date = local_date(now, limits.timezone)
    with uow.savepoint():
        existing = uow.quota_reservations.get_live_for_outbound(outbound.outbound_id)
        if existing is not None:
            if existing.state is QuotaReservationState.CONSUMED or existing.policy_date >= policy_date:
                raise DuplicateQuotaReservationError(existing)
            release_reservation(uow, existing, now)
        return _reserve(uow, limits, outbound, reservation_id=reservation_id, now=now, policy_date=policy_date)


def consume_reservation(uow: UnitOfWork, reservation: QuotaReservation, now: datetime) -> QuotaReservation:
    """ACTIVE -> CONSUMED when the message enters the send ledger (status SENDING).

    From then on the ledger counts the message and the reservation is ignored by
    ``build_quota_snapshot``, so the slot is never counted twice. Call it in the same
    transaction that moves the message to SENDING.
    """
    if reservation.state is not QuotaReservationState.ACTIVE:
        raise PolicyError(f"only ACTIVE reservations can be consumed (got {reservation.state})")
    consumed = QuotaReservation.model_validate(
        reservation.model_dump()
        | {"state": QuotaReservationState.CONSUMED, "updated_at": max(now, reservation.updated_at), "version": reservation.version + 1}
    )
    uow.quota_reservations.update(consumed, expected_version=reservation.version)
    return consumed


def _reserve(
    uow: UnitOfWork,
    limits: LimitPolicy,
    outbound: OutboundMessage,
    *,
    reservation_id: str,
    now: datetime,
    policy_date: date,
) -> QuotaReservation:
    # Counts are read here, after any stale release, never before it.
    campaign, mailbox = _resolve_campaign_and_mailbox(uow, outbound)
    day_start, day_end = local_day_bounds_utc(policy_date, limits.timezone)

    entries: list[LedgerEntry] = uow.outbound.list_ledger_entries(COUNTED_STATUSES, day_start, day_end)
    entries += uow.outbound.list_ledger_entries_for_contact(outbound.contact_id, COUNTED_STATUSES)
    reservations = uow.quota_reservations.list_active_for_date(policy_date)
    reservations += uow.quota_reservations.list_active_for_contact(outbound.contact_id)

    snapshot = build_quota_snapshot(
        entries,
        reservations,
        now=now,
        timezone=limits.timezone,
        mailbox=mailbox,
        campaign_id=outbound.campaign_id,
        contact_id=outbound.contact_id,
    )
    failures = evaluate_quota(snapshot, limits, outbound.kind, campaign)
    if failures:
        raise QuotaExceededError(failures)

    reservation = QuotaReservation(
        reservation_id=reservation_id,
        outbound_id=outbound.outbound_id,
        kind=outbound.kind,
        policy_date=policy_date,
        timezone=limits.timezone,
        mailbox=mailbox,
        campaign_id=outbound.campaign_id,
        contact_id=outbound.contact_id,
        state=QuotaReservationState.ACTIVE,
        created_at=now,
        updated_at=now,
    )
    try:
        uow.quota_reservations.add(reservation)
    except AlreadyExistsError:
        live = uow.quota_reservations.get_live_for_outbound(outbound.outbound_id)
        if live is None:
            raise  # reservation_id collision, not a duplicate reservation
        raise DuplicateQuotaReservationError(live) from None
    return reservation


def _resolve_campaign_and_mailbox(
    uow: UnitOfWork, outbound: OutboundMessage
) -> tuple[Campaign | None, str]:
    if outbound.campaign_id is not None:
        campaign = uow.campaigns.get(outbound.campaign_id)
        if campaign is None:
            raise PolicyError(f"campaign {outbound.campaign_id} not found")
        return campaign, campaign.sending_mailbox
    if outbound.kind is not OutboundKind.REPLY or outbound.thread_id is None:
        raise PolicyError("a message without a campaign must be a threaded REPLY")
    thread = uow.threads.get(outbound.thread_id)
    if thread is None:
        raise PolicyError(f"thread {outbound.thread_id} not found")
    return None, thread.mailbox
