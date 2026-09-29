"""Policy test builders. T0 (2026-01-01 12:00 UTC) is a Thursday, 14:00 in Europe/Kyiv."""

from datetime import datetime, time, timedelta

from app.core.enums import CampaignStatus, OutboundDecision, OutboundKind, OutboundStatus
from app.core.models import Campaign, OutboundMessage
from app.persistence.records import LedgerEntry, QuotaReservation
from app.policy import (
    CampaignLimits,
    GlobalDailyLimits,
    KillSwitchState,
    LimitPolicy,
    MailboxLimits,
    ScopedDailyLimits,
    SendingWindow,
    Weekday,
)
from tests.persistence import factories as f

TZ = "Europe/Kyiv"
T0 = f.T0
MAILBOX = "outreach@ourco.example"
WEEKDAYS = (Weekday.MONDAY, Weekday.TUESDAY, Weekday.WEDNESDAY, Weekday.THURSDAY, Weekday.FRIDAY)


def window(**overrides: object) -> SendingWindow:
    base: dict[str, object] = {
        "timezone": TZ,
        "working_days": WEEKDAYS,
        "start_local_time": time(9, 0),
        "end_local_time": time(18, 0),
    }
    return SendingWindow.model_validate(base | overrides)


def limits(
    *,
    sends: int = 100,
    new_contacts: int = 50,
    follow_ups: int = 50,
    per_contact: int = 3,
    interval: timedelta = timedelta(days=2),
    mailboxes: tuple[MailboxLimits, ...] = (),
    campaigns: tuple[CampaignLimits, ...] = (),
) -> LimitPolicy:
    return LimitPolicy(
        policy_version="policy-test-1",
        timezone=TZ,
        global_limits=GlobalDailyLimits(
            max_sends_per_day=sends, max_new_contacts_per_day=new_contacts, max_follow_ups_per_day=follow_ups
        ),
        mailboxes=mailboxes,
        campaigns=campaigns,
        max_follow_ups_per_contact=per_contact,
        min_interval_between_follow_ups=interval,
    )


def mailbox_limits(mailbox: str = MAILBOX, **values: int) -> MailboxLimits:
    return MailboxLimits(mailbox=mailbox, limits=ScopedDailyLimits(**values))


def campaign_limits(campaign_id: str = f.CAMPAIGN_ID, **values: int) -> CampaignLimits:
    return CampaignLimits(campaign_id=campaign_id, limits=ScopedDailyLimits(**values))


def kill_switch(enabled: bool = False) -> KillSwitchState:
    return KillSwitchState(
        enabled=enabled,
        reason="operator stop" if enabled else None,
        changed_at=T0 - timedelta(days=1),
        changed_by="operator-1",
    )


def active_campaign(**overrides: object) -> Campaign:
    return f.campaign(**({"status": CampaignStatus.ACTIVE, "activated_by": "operator-1"} | overrides))


def entry(
    outbound_id: str,
    *,
    kind: OutboundKind = OutboundKind.FIRST_TOUCH,
    status: OutboundStatus = OutboundStatus.SENT,
    sending_at: datetime = T0,
    mailbox: str = MAILBOX,
    campaign_id: str | None = f.CAMPAIGN_ID,
    contact_id: str = f.CONTACT_ID,
) -> LedgerEntry:
    return LedgerEntry(
        outbound_id=outbound_id,
        kind=kind,
        status=status,
        contact_id=contact_id,
        campaign_id=campaign_id,
        mailbox=mailbox,
        sending_at=sending_at,
    )


def reservation(
    reservation_id: str,
    outbound_id: str,
    *,
    kind: OutboundKind = OutboundKind.FIRST_TOUCH,
    policy_date: object = None,
    mailbox: str = MAILBOX,
    campaign_id: str | None = f.CAMPAIGN_ID,
    contact_id: str = f.CONTACT_ID,
    **overrides: object,
) -> QuotaReservation:
    base: dict[str, object] = {
        "reservation_id": reservation_id,
        "outbound_id": outbound_id,
        "kind": kind,
        "policy_date": policy_date or T0.date(),
        "timezone": TZ,
        "mailbox": mailbox,
        "campaign_id": campaign_id,
        "contact_id": contact_id,
        "created_at": T0,
        "updated_at": T0,
    }
    return QuotaReservation.model_validate(base | overrides)


def approved_message(outbound_id: str, **overrides: object) -> OutboundMessage:
    base: dict[str, object] = {
        "outbound_id": outbound_id,
        "idempotency_key": f"key:{outbound_id}",
        "status": OutboundStatus.APPROVED,
        "decision": OutboundDecision.SEND,
        "send_permit_id": f"permit-{outbound_id}",
        "approved_at": T0,
    }
    return f.outbound_message(**(base | overrides))


def sent_message(outbound_id: str, *, sending_at: datetime = T0, **overrides: object) -> OutboundMessage:
    return approved_message(
        outbound_id,
        **(
            {
                "status": OutboundStatus.SENT,
                "created_at": sending_at,
                "approved_at": sending_at,
                "sending_at": sending_at,
                "sent_at": sending_at,
            }
            | overrides
        ),
    )
