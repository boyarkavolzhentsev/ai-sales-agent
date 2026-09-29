from datetime import UTC, datetime, time, timedelta

import pytest
from pydantic import ValidationError

from app.core.enums import DNCReason, DNCScope
from app.policy import (
    LimitPolicy,
    Weekday,
    evaluate_suppression,
    is_within_sending_window,
)
from app.policy.limits import effective_max_follow_ups, effective_min_interval
from app.policy.models import PolicyReason
from app.policy.windows import local_date, local_day_bounds_utc
from tests.persistence import factories as f
from tests.policy import builders as b


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


# ---- A. Sending windows (Europe/Kyiv, Mon-Fri 09:00-18:00) -------------------------


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (utc(2026, 1, 1, 12, 0), True),  # Thu 14:00 local
        (utc(2026, 1, 1, 7, 0), True),  # Thu 09:00 local: start is inclusive
        (utc(2026, 1, 1, 6, 59), False),  # Thu 08:59 local: before
        (utc(2026, 1, 1, 16, 0), False),  # Thu 18:00 local: end is exclusive
        (utc(2026, 1, 1, 17, 30), False),  # Thu 19:30 local: after
        (utc(2026, 1, 3, 12, 0), False),  # Sat: non-working day
        (utc(2026, 1, 4, 12, 0), False),  # Sun: non-working day
        (utc(2026, 1, 4, 22, 30), False),  # Sun 22:30 UTC = Mon 00:30 local: before start
        (utc(2026, 1, 5, 7, 30), True),  # Mon 09:30 local
    ],
)
def test_window_membership(now: datetime, expected: bool) -> None:
    assert is_within_sending_window(now, b.window()) is expected


def test_window_is_dst_safe() -> None:
    # Kyiv switches from UTC+2 to UTC+3 on 2026-03-29. 06:30 UTC is 08:30 local on the
    # Friday before (outside) and 09:30 local on the Monday after (inside).
    assert not is_within_sending_window(utc(2026, 3, 27, 6, 30), b.window())
    assert is_within_sending_window(utc(2026, 3, 30, 6, 30), b.window())
    # Same local 17:30 on both sides of the switch is inside the window.
    assert is_within_sending_window(utc(2026, 3, 27, 15, 30), b.window())
    assert is_within_sending_window(utc(2026, 3, 30, 14, 30), b.window())


def test_window_accepts_non_utc_aware_input() -> None:
    other = datetime(2026, 1, 1, 7, 0, tzinfo=UTC).astimezone()  # any aware offset
    assert is_within_sending_window(other, b.window()) is True


def test_window_rejects_naive_now() -> None:
    with pytest.raises(ValueError):
        is_within_sending_window(datetime(2026, 1, 1, 12, 0), b.window())


@pytest.mark.parametrize(
    ("start", "end"),
    [(time(22, 0), time(6, 0)), (time(9, 0), time(9, 0))],
)
def test_overnight_and_empty_windows_rejected(start: time, end: time) -> None:
    with pytest.raises(ValidationError, match="overnight"):
        b.window(start_local_time=start, end_local_time=end)


def test_window_validation() -> None:
    with pytest.raises(ValidationError):
        b.window(timezone="Mars/Olympus")
    with pytest.raises(ValidationError):
        b.window(working_days=())
    with pytest.raises(ValidationError, match="duplicate"):
        b.window(working_days=(Weekday.MONDAY, Weekday.MONDAY))
    with pytest.raises(ValidationError, match="naive"):
        b.window(start_local_time=time(9, 0, tzinfo=UTC))


def test_local_day_bounds_handle_dst() -> None:
    start, end = local_day_bounds_utc(local_date(utc(2026, 3, 29, 12), b.TZ), b.TZ)
    assert start == utc(2026, 3, 28, 22)
    assert end - start == timedelta(hours=23)  # the spring-forward day is 23 hours
    assert local_date(utc(2025, 12, 31, 22, 30), b.TZ).isoformat() == "2026-01-01"


# ---- D. Limit policy validation ----------------------------------------------------


def test_valid_limit_policy() -> None:
    policy = b.limits(
        mailboxes=(b.mailbox_limits(max_sends_per_day=40),),
        campaigns=(b.campaign_limits(max_new_contacts_per_day=10),),
    )
    assert policy.limits_for_mailbox(b.MAILBOX) is not None
    assert policy.limits_for_campaign("other") is None
    assert policy.timezone == "Europe/Kyiv"


def test_negative_limits_rejected() -> None:
    with pytest.raises(ValidationError):
        b.limits(sends=-1)
    with pytest.raises(ValidationError):
        b.limits(per_contact=-1)
    with pytest.raises(ValidationError):
        b.mailbox_limits(max_sends_per_day=-5)


@pytest.mark.parametrize("interval", [timedelta(0), timedelta(seconds=-1)])
def test_interval_must_be_positive(interval: timedelta) -> None:
    with pytest.raises(ValidationError, match="min_interval"):
        b.limits(interval=interval)


def test_child_scopes_may_be_stricter_but_not_looser() -> None:
    b.limits(sends=100, mailboxes=(b.mailbox_limits(max_sends_per_day=100),))
    with pytest.raises(ValidationError, match="exceeds the global cap"):
        b.limits(sends=100, mailboxes=(b.mailbox_limits(max_sends_per_day=101),))
    with pytest.raises(ValidationError, match="exceeds the global cap"):
        b.limits(follow_ups=5, campaigns=(b.campaign_limits(max_follow_ups_per_day=6),))


def test_scoped_keys_unique() -> None:
    with pytest.raises(ValidationError, match="duplicate"):
        b.limits(mailboxes=(b.mailbox_limits(), b.mailbox_limits(mailbox="OUTREACH@ourco.example")))
    with pytest.raises(ValidationError, match="duplicate"):
        b.limits(campaigns=(b.campaign_limits(), b.campaign_limits()))


def test_timezone_must_be_explicit_and_valid() -> None:
    data = b.limits().model_dump()
    del data["timezone"]
    with pytest.raises(ValidationError):
        LimitPolicy.model_validate(data)
    with pytest.raises(ValidationError):
        LimitPolicy.model_validate(data | {"timezone": "Not/AZone"})


def test_effective_follow_up_caps_pick_the_stricter_value() -> None:
    campaign = b.active_campaign(max_follow_ups=2, min_interval_between_follow_ups=timedelta(days=1))
    assert effective_max_follow_ups(b.limits(per_contact=3), campaign) == 2
    assert effective_max_follow_ups(b.limits(per_contact=1), campaign) == 1
    assert effective_min_interval(b.limits(interval=timedelta(days=2)), campaign) == timedelta(days=2)


# ---- B. Suppression -------------------------------------------------------------------


EMAIL = "partners@prospect.example"


def test_email_match() -> None:
    match = evaluate_suppression(EMAIL, None, [f.dnc_entry()], b.T0)
    assert match is not None
    assert (match.scope, match.policy_reason) == (DNCScope.EMAIL, PolicyReason.DNC_EMAIL)


def test_domain_match_on_email_domain_and_company_domain() -> None:
    by_email_domain = f.dnc_entry(entry_id="d1", scope=DNCScope.DOMAIN, value="prospect.example")
    match = evaluate_suppression(EMAIL, None, [by_email_domain], b.T0)
    assert match is not None and match.policy_reason is PolicyReason.DNC_DOMAIN
    by_company = f.dnc_entry(entry_id="d2", scope=DNCScope.DOMAIN, value="parent-group.example")
    assert evaluate_suppression(EMAIL, None, [by_company], b.T0) is None
    assert evaluate_suppression(EMAIL, "Parent-Group.EXAMPLE", [by_company], b.T0) is not None


def test_case_normalization() -> None:
    assert evaluate_suppression("PARTNERS@Prospect.Example", None, [f.dnc_entry()], b.T0) is not None


def test_expired_entry_ignored_and_boundary_is_exclusive() -> None:
    expiring = f.dnc_entry(expires_at=b.T0 + timedelta(hours=1))
    assert evaluate_suppression(EMAIL, None, [expiring], b.T0) is not None
    assert evaluate_suppression(EMAIL, None, [expiring], b.T0 + timedelta(hours=1)) is None
    assert evaluate_suppression(EMAIL, None, [expiring], b.T0 + timedelta(days=1)) is None


def test_permanent_entry_blocks_forever() -> None:
    assert evaluate_suppression(EMAIL, None, [f.dnc_entry()], b.T0 + timedelta(days=3650)) is not None


def test_unrelated_entries_ignored() -> None:
    entries = [
        f.dnc_entry(entry_id="x1", value="someone@prospect.example"),
        f.dnc_entry(entry_id="x2", scope=DNCScope.DOMAIN, value="other.example"),
        f.dnc_entry(entry_id="x3", scope=DNCScope.DOMAIN, value="sub.prospect.example"),
    ]
    assert evaluate_suppression(EMAIL, None, entries, b.T0) is None


def test_most_specific_then_most_restrictive_match_wins() -> None:
    domain_permanent = f.dnc_entry(entry_id="a", scope=DNCScope.DOMAIN, value="prospect.example")
    email_expiring = f.dnc_entry(entry_id="b", expires_at=b.T0 + timedelta(days=1))
    email_later = f.dnc_entry(entry_id="c", expires_at=b.T0 + timedelta(days=9), reason=DNCReason.COMPLAINT)
    email_permanent = f.dnc_entry(entry_id="d", reason=DNCReason.LEGAL)
    match = evaluate_suppression(EMAIL, None, [domain_permanent, email_expiring, email_later], b.T0)
    assert match is not None and match.entry_id == "c"
    match = evaluate_suppression(EMAIL, None, [email_later, email_permanent, domain_permanent], b.T0)
    assert match is not None and match.entry_id == "d"
    match = evaluate_suppression(EMAIL, None, [domain_permanent], b.T0)
    assert match is not None and match.entry_id == "a"


def test_suppression_requires_aware_now_and_valid_email() -> None:
    with pytest.raises(ValueError):
        evaluate_suppression(EMAIL, None, [f.dnc_entry()], datetime(2026, 1, 1))
    with pytest.raises(ValueError):
        evaluate_suppression("not-an-email", None, [], b.T0)
