from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta

import pytest

from app.core.enums import OutboundKind, OutboundStatus
from app.persistence.records import LedgerEntry, QuotaReservation, QuotaReservationState
from app.policy import (
    COUNTED_STATUSES,
    PolicyReason,
    QuotaSnapshot,
    build_quota_snapshot,
    evaluate_quota,
)
from app.policy.quota import ledger_entry_from_message
from tests.persistence import factories as f
from tests.policy import builders as b

FU = OutboundKind.FOLLOW_UP
FT = OutboundKind.FIRST_TOUCH


def snapshot(
    entries: Iterable[LedgerEntry] = (),
    reservations: Iterable[QuotaReservation] = (),
    *,
    now: datetime = b.T0,
    campaign_id: str | None = f.CAMPAIGN_ID,
    mailbox: str = b.MAILBOX,
) -> QuotaSnapshot:
    return build_quota_snapshot(
        entries,
        reservations,
        now=now,
        timezone=b.TZ,
        mailbox=mailbox,
        campaign_id=campaign_id,
        contact_id=f.CONTACT_ID,
    )


# ---- C. Quota calculations ----------------------------------------------------------


def test_counted_statuses_are_exactly_the_dispatched_ones() -> None:
    assert COUNTED_STATUSES == {
        OutboundStatus.SENDING,
        OutboundStatus.SENT,
        OutboundStatus.BOUNCED,
        OutboundStatus.FAILED,
    }


@pytest.mark.parametrize("status", list(OutboundStatus))
def test_only_counted_statuses_count(status: OutboundStatus) -> None:
    result = snapshot([b.entry("o1", status=status)])
    assert result.sends_today == (1 if status in COUNTED_STATUSES else 0)


def test_counts_by_kind() -> None:
    result = snapshot(
        [
            b.entry("o1", kind=FT),
            b.entry("o2", kind=FT),
            b.entry("o3", kind=FU),
            b.entry("o4", kind=OutboundKind.REPLY, campaign_id=None),
        ]
    )
    assert (result.sends_today, result.new_contacts_today, result.follow_ups_today) == (4, 2, 1)
    assert result.sends_today_for_campaign == 3


def test_timezone_date_boundary() -> None:
    # Kyiv midnight on 2026-01-01 is 2025-12-31 22:00 UTC.
    result = snapshot(
        [
            b.entry("before-midnight", sending_at=datetime(2025, 12, 31, 21, 59, tzinfo=UTC)),
            b.entry("after-midnight", sending_at=datetime(2025, 12, 31, 22, 0, tzinfo=UTC)),
            b.entry("last-minute", sending_at=datetime(2026, 1, 1, 21, 59, tzinfo=UTC)),
            b.entry("next-day", sending_at=datetime(2026, 1, 1, 22, 0, tzinfo=UTC)),
        ]
    )
    assert result.policy_date == date(2026, 1, 1)
    assert result.sends_today == 2


def test_mailbox_scope() -> None:
    result = snapshot(
        [
            b.entry("o1"),
            b.entry("o2", mailbox="other@ourco.example", campaign_id="camp-2"),
            b.entry("o3", mailbox="OTHER@ourco.example", campaign_id="camp-2"),
        ]
    )
    assert (result.sends_today, result.sends_today_for_mailbox) == (3, 1)


def test_campaign_scope() -> None:
    result = snapshot([b.entry("o1"), b.entry("o2", campaign_id="camp-2"), b.entry("o3", campaign_id=None)])
    assert (result.sends_today, result.sends_today_for_campaign) == (3, 1)
    assert snapshot([b.entry("o1")], campaign_id=None).campaign_counts is None


def test_contact_follow_up_count_is_all_time_per_campaign() -> None:
    long_ago = b.T0 - timedelta(days=40)
    result = snapshot(
        [
            b.entry("fu1", kind=FU, sending_at=long_ago),
            b.entry("fu2", kind=FU),
            b.entry("ft", kind=FT),
            b.entry("fu-other-campaign", kind=FU, campaign_id="camp-2"),
            b.entry("fu-other-contact", kind=FU, contact_id="contact-2"),
            b.entry("fu-cancelled", kind=FU, status=OutboundStatus.CANCELLED),
        ]
    )
    assert result.follow_ups_for_contact == 2
    assert result.follow_ups_today == 3  # fu2 plus the other-campaign and other-contact ones


def test_active_reservations_count_until_their_message_is_in_the_ledger() -> None:
    reservations = [
        b.reservation("r1", "pending-1"),
        b.reservation("r2", "already-sent"),
        b.reservation("r3", "released", state=QuotaReservationState.RELEASED),
        b.reservation("r4", "consumed", state=QuotaReservationState.CONSUMED),
        b.reservation("r5", "yesterday", policy_date=date(2025, 12, 31)),
        b.reservation("r1", "pending-1"),  # duplicate listing is counted once
    ]
    result = snapshot([b.entry("already-sent")], reservations)
    assert result.sends_today == 2  # already-sent (ledger) + pending-1 (reservation)


def test_duplicate_ledger_entries_counted_once() -> None:
    assert snapshot([b.entry("o1"), b.entry("o1")]).sends_today == 1


def test_ledger_entry_from_message() -> None:
    assert ledger_entry_from_message(b.approved_message("o1"), b.MAILBOX) is None
    entry = ledger_entry_from_message(b.sent_message("o2"), b.MAILBOX)
    assert entry is not None and entry.sending_at == b.T0


def test_snapshot_requires_aware_now() -> None:
    with pytest.raises(ValueError):
        snapshot(now=datetime(2026, 1, 1, 12))


# ---- Limit evaluation over a snapshot -----------------------------------------------


def test_limits_not_reached() -> None:
    assert evaluate_quota(snapshot([b.entry("o1")]), b.limits(sends=2), FT, b.active_campaign()) == ()


def test_limit_reached_at_count_equal_to_limit_and_zero_blocks_all() -> None:
    campaign = b.active_campaign()
    [check] = evaluate_quota(snapshot([b.entry("o1")]), b.limits(sends=1), OutboundKind.REPLY, campaign)
    assert check.reason is PolicyReason.GLOBAL_DAILY_LIMIT
    reasons = {c.reason for c in evaluate_quota(snapshot(), b.limits(new_contacts=0), FT, campaign)}
    assert reasons == {PolicyReason.NEW_CONTACT_DAILY_LIMIT}


def test_scoped_limits_produce_scoped_reasons() -> None:
    policy = b.limits(
        mailboxes=(b.mailbox_limits(max_sends_per_day=1),),
        campaigns=(b.campaign_limits(max_sends_per_day=1, max_follow_ups_per_day=1),),
    )
    reasons = [c.reason for c in evaluate_quota(snapshot([b.entry("o1", kind=FU)]), policy, FU, b.active_campaign())]
    assert reasons == [
        PolicyReason.MAILBOX_DAILY_LIMIT,
        PolicyReason.CAMPAIGN_DAILY_LIMIT,
        PolicyReason.FOLLOWUP_DAILY_LIMIT,
    ]


def test_kind_specific_limits_only_apply_to_their_kind() -> None:
    full = snapshot([b.entry("o1", kind=FT), b.entry("o2", kind=FU)])
    policy = b.limits(new_contacts=1, follow_ups=1)
    campaign = b.active_campaign()
    assert {c.reason for c in evaluate_quota(full, policy, FT, campaign)} == {PolicyReason.NEW_CONTACT_DAILY_LIMIT}
    assert PolicyReason.NEW_CONTACT_DAILY_LIMIT not in {c.reason for c in evaluate_quota(full, policy, FU, campaign)}
    assert evaluate_quota(full, policy, OutboundKind.REPLY, campaign) == ()


def test_contact_follow_up_cap_uses_stricter_of_policy_and_campaign() -> None:
    two = snapshot([b.entry("fu1", kind=FU, sending_at=b.T0 - timedelta(days=5)), b.entry("fu2", kind=FU, sending_at=b.T0 - timedelta(days=3))])
    reasons = {c.reason for c in evaluate_quota(two, b.limits(per_contact=5), FU, b.active_campaign(max_follow_ups=2))}
    assert reasons == {PolicyReason.CONTACT_FOLLOWUP_LIMIT}
    assert evaluate_quota(two, b.limits(per_contact=5), FU, b.active_campaign(max_follow_ups=3)) == ()


def test_evaluate_quota_rejects_mismatched_campaign() -> None:
    with pytest.raises(ValueError):
        evaluate_quota(snapshot(), b.limits(), FT, b.active_campaign(campaign_id="camp-2"))
