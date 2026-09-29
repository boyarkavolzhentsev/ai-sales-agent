"""Pure quota counting over send-ledger facts. No SQL, no mutable counters.

Counted statuses: SENDING, SENT, BOUNCED and FAILED. This refines the Stage 0 set
(SENDING/SENT/BOUNCED) by also counting FAILED: a failed attempt may still have been
accepted by the provider, so counting it is the conservative choice. A retried message
leaves FAILED, so it is never counted twice. Every counted status has ``sending_at``,
which attributes the message to a local policy date.

Not counted: DRAFTED, PENDING_REVIEW, HELD, APPROVED, CANCELLED, SKIPPED. Messages that
are approved but not yet dispatched are covered by ACTIVE quota reservations instead.
"""

from collections.abc import Iterable
from datetime import date, datetime

from pydantic import NonNegativeInt

from app.core.enums import OutboundKind, OutboundStatus
from app.core.models import Campaign, OutboundMessage
from app.core.models.base import CoreModel
from app.core.models.types import EmailAddress, EntityId
from app.persistence.records import LedgerEntry, QuotaReservation, QuotaReservationState
from app.policy.limits import LimitPolicy, ScopedDailyLimits, effective_max_follow_ups
from app.policy.models import PolicyCheck, PolicyReason
from app.policy.windows import TimezoneName, local_date

COUNTED_STATUSES: frozenset[OutboundStatus] = frozenset(
    {OutboundStatus.SENDING, OutboundStatus.SENT, OutboundStatus.BOUNCED, OutboundStatus.FAILED}
)


class ScopeCounts(CoreModel):
    """Messages counted today within one scope, by kind of limit."""

    sends: NonNegativeInt = 0
    new_contacts: NonNegativeInt = 0
    follow_ups: NonNegativeInt = 0


class QuotaSnapshot(CoreModel):
    """Effective usage (ledger + active reservations) for one prospective message."""

    policy_date: date
    timezone: TimezoneName
    mailbox: EmailAddress
    campaign_id: EntityId | None = None
    contact_id: EntityId
    global_counts: ScopeCounts
    mailbox_counts: ScopeCounts
    campaign_counts: ScopeCounts | None = None
    follow_ups_for_contact: NonNegativeInt

    @property
    def sends_today(self) -> int:
        return self.global_counts.sends

    @property
    def new_contacts_today(self) -> int:
        return self.global_counts.new_contacts

    @property
    def follow_ups_today(self) -> int:
        return self.global_counts.follow_ups

    @property
    def sends_today_for_mailbox(self) -> int:
        return self.mailbox_counts.sends

    @property
    def sends_today_for_campaign(self) -> int:
        return self.campaign_counts.sends if self.campaign_counts is not None else 0


def ledger_entry_from_message(message: OutboundMessage, mailbox: str) -> LedgerEntry | None:
    """Project a message into a ledger entry, or None if its status is not counted."""
    if message.status not in COUNTED_STATUSES or message.sending_at is None:
        return None
    return LedgerEntry(
        outbound_id=message.outbound_id,
        kind=message.kind,
        status=message.status,
        contact_id=message.contact_id,
        campaign_id=message.campaign_id,
        mailbox=mailbox,
        sending_at=message.sending_at,
    )


class _Usage:
    """Mutable tally used only while building a snapshot."""

    def __init__(self) -> None:
        self.sends = 0
        self.new_contacts = 0
        self.follow_ups = 0

    def add(self, kind: OutboundKind) -> None:
        self.sends += 1
        if kind is OutboundKind.FIRST_TOUCH:
            self.new_contacts += 1
        elif kind is OutboundKind.FOLLOW_UP:
            self.follow_ups += 1

    def freeze(self) -> ScopeCounts:
        return ScopeCounts(sends=self.sends, new_contacts=self.new_contacts, follow_ups=self.follow_ups)


def build_quota_snapshot(
    entries: Iterable[LedgerEntry],
    reservations: Iterable[QuotaReservation],
    *,
    now: datetime,
    timezone: str,
    mailbox: str,
    campaign_id: str | None,
    contact_id: str,
) -> QuotaSnapshot:
    """Count ledger entries and ACTIVE reservations for today and for the contact.

    Entries in non-counted statuses are ignored. Duplicate entries (same outbound_id) are
    counted once. A reservation is ignored once its message appears in the ledger.
    """
    today = local_date(now, timezone)
    counted: dict[str, LedgerEntry] = {}
    for entry in entries:
        if entry.status in COUNTED_STATUSES:
            counted.setdefault(entry.outbound_id, entry)

    usage = {"global": _Usage(), "mailbox": _Usage(), "campaign": _Usage()}
    contact_follow_ups = 0

    def tally(kind: OutboundKind, day: date, box: str, campaign: str | None, contact: str) -> None:
        nonlocal contact_follow_ups
        if day == today:
            usage["global"].add(kind)
            if box == mailbox:
                usage["mailbox"].add(kind)
            if campaign_id is not None and campaign == campaign_id:
                usage["campaign"].add(kind)
        if kind is OutboundKind.FOLLOW_UP and contact == contact_id and campaign == campaign_id:
            contact_follow_ups += 1

    for entry in counted.values():
        tally(entry.kind, local_date(entry.sending_at, timezone), entry.mailbox, entry.campaign_id, entry.contact_id)

    seen_reservations: set[str] = set()
    for reservation in reservations:
        if (
            reservation.state is not QuotaReservationState.ACTIVE
            or reservation.outbound_id in counted
            or reservation.reservation_id in seen_reservations
        ):
            continue
        seen_reservations.add(reservation.reservation_id)
        tally(
            reservation.kind,
            reservation.policy_date,
            reservation.mailbox,
            reservation.campaign_id,
            reservation.contact_id,
        )

    return QuotaSnapshot(
        policy_date=today,
        timezone=timezone,
        mailbox=mailbox,
        campaign_id=campaign_id,
        contact_id=contact_id,
        global_counts=usage["global"].freeze(),
        mailbox_counts=usage["mailbox"].freeze(),
        campaign_counts=usage["campaign"].freeze() if campaign_id is not None else None,
        follow_ups_for_contact=contact_follow_ups,
    )


_SEND_REASONS = {
    "global": PolicyReason.GLOBAL_DAILY_LIMIT,
    "mailbox": PolicyReason.MAILBOX_DAILY_LIMIT,
    "campaign": PolicyReason.CAMPAIGN_DAILY_LIMIT,
}


def evaluate_quota(
    snapshot: QuotaSnapshot,
    limits: LimitPolicy,
    kind: OutboundKind,
    campaign: Campaign | None,
) -> tuple[PolicyCheck, ...]:
    """Every limit that one more message of ``kind`` would exceed.

    A limit L is exhausted when the effective count is already >= L (so L=0 blocks all).
    """
    if campaign is not None and campaign.campaign_id != snapshot.campaign_id:
        raise ValueError("snapshot and campaign refer to different campaigns")
    global_scoped = ScopedDailyLimits(**limits.global_limits.model_dump())
    scopes: list[tuple[str, ScopeCounts, ScopedDailyLimits | None]] = [
        ("global", snapshot.global_counts, global_scoped),
        ("mailbox", snapshot.mailbox_counts, limits.limits_for_mailbox(snapshot.mailbox)),
    ]
    if snapshot.campaign_id is not None and snapshot.campaign_counts is not None:
        scopes.append(
            ("campaign", snapshot.campaign_counts, limits.limits_for_campaign(snapshot.campaign_id))
        )

    failures: list[PolicyCheck] = []
    for scope, counts, scoped in scopes:
        if scoped is None:
            continue
        if _exhausted(counts.sends, scoped.max_sends_per_day):
            failures.append(
                PolicyCheck(reason=_SEND_REASONS[scope], detail=f"{scope} sends {counts.sends}/{scoped.max_sends_per_day}")
            )
        if kind is OutboundKind.FIRST_TOUCH and _exhausted(counts.new_contacts, scoped.max_new_contacts_per_day):
            failures.append(
                PolicyCheck(
                    reason=PolicyReason.NEW_CONTACT_DAILY_LIMIT,
                    detail=f"{scope} new contacts {counts.new_contacts}/{scoped.max_new_contacts_per_day}",
                )
            )
        if kind is OutboundKind.FOLLOW_UP and _exhausted(counts.follow_ups, scoped.max_follow_ups_per_day):
            failures.append(
                PolicyCheck(
                    reason=PolicyReason.FOLLOWUP_DAILY_LIMIT,
                    detail=f"{scope} follow-ups {counts.follow_ups}/{scoped.max_follow_ups_per_day}",
                )
            )
    if kind is OutboundKind.FOLLOW_UP:
        if campaign is None:
            raise ValueError("follow-up quota evaluation requires the campaign")
        cap = effective_max_follow_ups(limits, campaign)
        if snapshot.follow_ups_for_contact >= cap:
            failures.append(
                PolicyCheck(
                    reason=PolicyReason.CONTACT_FOLLOWUP_LIMIT,
                    detail=f"contact follow-ups {snapshot.follow_ups_for_contact}/{cap}",
                )
            )
    return tuple(failures)


def _exhausted(count: int, limit: int | None) -> bool:
    return limit is not None and count >= limit
