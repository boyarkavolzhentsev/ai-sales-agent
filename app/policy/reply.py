"""Stage 3 outbound policy for a threaded REPLY, gathered from persistence.

Shared by dispatch (Stage 8) and follow-up execution (Stage 9) so both apply exactly
the same checks: suppression (email, email domain, company domain), bounced address,
kill switch, sending window and quota. Reads only; never reserves or writes.
"""

from datetime import datetime

from app.core.enums import DNCScope, OutboundKind
from app.core.models import Campaign, DoNotContactEntry, ProspectContact
from app.persistence import UnitOfWork
from app.policy.limits import LimitPolicy
from app.policy.models import KillSwitchState, PolicyDecisionResult
from app.policy.outbound import PolicyContext, evaluate_outbound_policy
from app.policy.quota import COUNTED_STATUSES, QuotaSnapshot, build_quota_snapshot
from app.policy.windows import SendingWindow, local_date, local_day_bounds_utc


def suppression_entries(uow: UnitOfWork, email: str, company_domain: str | None) -> list[DoNotContactEntry]:
    entries = list(uow.dnc.list_for_value(DNCScope.EMAIL, email))
    for domain in sorted({email.split("@", 1)[1], *([company_domain] if company_domain else [])}):
        entries.extend(uow.dnc.list_for_value(DNCScope.DOMAIN, domain))
    return entries


def evaluate_reply_policy(
    uow: UnitOfWork,
    *,
    contact: ProspectContact,
    company_domain: str | None,
    mailbox: str,
    limits: LimitPolicy,
    window: SendingWindow,
    kill_switch: KillSwitchState,
    now: datetime,
    exclude_outbound_id: str | None = None,
) -> PolicyDecisionResult:
    """``exclude_outbound_id``: a message whose own earlier ledger entry must not count
    against itself (a FAILED attempt being retried moves the same message back to SENDING,
    so it is counted once, never twice)."""
    return evaluate_send_policy(
        uow, kind=OutboundKind.REPLY, campaign=None, contact=contact, company_domain=company_domain, mailbox=mailbox,
        limits=limits, window=window, kill_switch=kill_switch, now=now, exclude_outbound_id=exclude_outbound_id,
    )


def evaluate_send_policy(
    uow: UnitOfWork,
    *,
    kind: OutboundKind,
    campaign: Campaign | None,
    contact: ProspectContact,
    company_domain: str | None,
    mailbox: str,
    limits: LimitPolicy,
    window: SendingWindow,
    kill_switch: KillSwitchState,
    now: datetime,
    exclude_outbound_id: str | None = None,
) -> PolicyDecisionResult:
    """Any outbound kind. Campaign messages (FIRST_TOUCH, FOLLOW_UP) also get the Stage 3
    campaign checks and the campaign, new-contact and follow-up quotas, from the
    campaign's own sending mailbox."""
    quota = quota_snapshot(uow, contact=contact, mailbox=mailbox, campaign=campaign, limits=limits, now=now,
                           exclude_outbound_id=exclude_outbound_id)
    return evaluate_outbound_policy(
        PolicyContext(
            now=now,
            kind=kind,
            contact=contact,
            company_domain=company_domain,
            campaign=campaign,
            suppression_entries=tuple(suppression_entries(uow, contact.email, company_domain)),
            kill_switch=kill_switch,
            window=window,
            limits=limits,
            quota=quota,
        )
    )


def quota_snapshot(
    uow: UnitOfWork, *, contact: ProspectContact, mailbox: str, campaign: Campaign | None, limits: LimitPolicy,
    now: datetime, exclude_outbound_id: str | None = None,
) -> QuotaSnapshot:
    """Ledger + ACTIVE reservations for today and for the contact (Stage 3 counting)."""
    tz = limits.timezone
    day_start, day_end = local_day_bounds_utc(local_date(now, tz), tz)
    entries = uow.outbound.list_ledger_entries(COUNTED_STATUSES, day_start, day_end)
    entries += uow.outbound.list_ledger_entries_for_contact(contact.contact_id, COUNTED_STATUSES)
    entries = [e for e in entries if e.outbound_id != exclude_outbound_id]
    reservations = uow.quota_reservations.list_active_for_date(local_date(now, tz))
    reservations += uow.quota_reservations.list_active_for_contact(contact.contact_id)
    return build_quota_snapshot(
        entries, reservations, now=now, timezone=tz, mailbox=mailbox,
        campaign_id=campaign.campaign_id if campaign is not None else None, contact_id=contact.contact_id,
    )
