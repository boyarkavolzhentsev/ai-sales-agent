from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.core.enums import (
    CampaignStatus,
    DNCScope,
    EmailValidity,
    FollowUpCancelReason,
    FollowUpStatus,
    LeadStage,
    LeadStatus,
    OutboundDecision,
    OutboundKind,
)
from app.policy import (
    REASON_DECISIONS,
    RESERVED_REASONS,
    KillSwitchState,
    PolicyCheck,
    PolicyContext,
    PolicyDecisionResult,
    PolicyReason,
    build_quota_snapshot,
    evaluate_follow_up_policy,
    evaluate_outbound_policy,
)
from app.persistence.records import LedgerEntry
from app.policy.quota import QuotaSnapshot
from tests.persistence import factories as f
from tests.policy import builders as b

SATURDAY = datetime(2026, 1, 3, 12, 0, tzinfo=UTC)


def quota(
    entries: Iterable[LedgerEntry] = (),
    *,
    campaign_id: str | None = f.CAMPAIGN_ID,
    mailbox: str = b.MAILBOX,
    now: datetime = b.T0,
) -> QuotaSnapshot:
    return build_quota_snapshot(
        entries, (), now=now, timezone=b.TZ, mailbox=mailbox, campaign_id=campaign_id, contact_id=f.CONTACT_ID
    )


def context(**overrides: object) -> PolicyContext:
    base: dict[str, object] = {
        "now": b.T0,
        "kind": OutboundKind.FIRST_TOUCH,
        "contact": f.contact(),
        "campaign": b.active_campaign(),
        "kill_switch": b.kill_switch(),
        "window": b.window(),
        "limits": b.limits(),
        "quota": quota(),
    }
    return PolicyContext.model_validate(base | overrides)


def decide(**overrides: object) -> PolicyDecisionResult:
    return evaluate_outbound_policy(context(**overrides))


# ---- Contracts ----------------------------------------------------------------------


def test_every_reason_has_a_fixed_decision() -> None:
    assert set(REASON_DECISIONS) == set(PolicyReason)
    assert RESERVED_REASONS <= set(PolicyReason)


def test_decision_result_must_follow_from_checks() -> None:
    hold = PolicyCheck(reason=PolicyReason.KILL_SWITCH, detail="x")
    assert PolicyDecisionResult.from_checks([]).decision is OutboundDecision.SEND
    assert PolicyDecisionResult.from_checks([hold]).decision is OutboundDecision.HOLD
    with pytest.raises(ValidationError, match="does not follow"):
        PolicyDecisionResult(decision=OutboundDecision.SEND, checks=(hold,))


def test_kill_switch_state_requires_reason_when_enabled() -> None:
    with pytest.raises(ValidationError):
        KillSwitchState(enabled=True, changed_at=b.T0, changed_by="op")


def test_context_consistency() -> None:
    with pytest.raises(ValidationError, match="requires the campaign"):
        context(campaign=None, quota=quota(campaign_id=None))
    with pytest.raises(ValidationError, match="different campaign"):
        context(quota=quota(campaign_id="camp-2"))
    with pytest.raises(ValidationError, match="different mailbox"):
        context(quota=quota(mailbox="other@ourco.example"))


# ---- Baseline SEND ------------------------------------------------------------------


def test_all_checks_pass_sends() -> None:
    result = decide()
    assert (result.decision, result.checks) == (OutboundDecision.SEND, ())


def test_reply_without_campaign_can_send() -> None:
    result = decide(kind=OutboundKind.REPLY, campaign=None, quota=quota(campaign_id=None))
    assert result.decision is OutboundDecision.SEND


# ---- E. Kill switch -----------------------------------------------------------------


def test_kill_switch_enabled_holds_and_disabled_continues() -> None:
    held = decide(kill_switch=b.kill_switch(enabled=True))
    assert (held.decision, held.reasons) == (OutboundDecision.HOLD, (PolicyReason.KILL_SWITCH,))
    assert decide(kill_switch=b.kill_switch(enabled=False)).decision is OutboundDecision.SEND
    reply = decide(kind=OutboundKind.REPLY, campaign=None, quota=quota(campaign_id=None), kill_switch=b.kill_switch(True))
    assert reply.decision is OutboundDecision.HOLD  # automated replies stop too


# ---- F. Campaign state ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "decision", "reason"),
    [
        (CampaignStatus.DRAFT, OutboundDecision.HOLD, PolicyReason.CAMPAIGN_NOT_ACTIVE),
        (CampaignStatus.PAUSED, OutboundDecision.HOLD, PolicyReason.CAMPAIGN_PAUSED),
        (CampaignStatus.ENDED, OutboundDecision.SKIP, PolicyReason.CAMPAIGN_ENDED),
    ],
)
def test_campaign_status(status: CampaignStatus, decision: OutboundDecision, reason: PolicyReason) -> None:
    overrides: dict[str, object] = {"status": status}
    if status is CampaignStatus.DRAFT:
        overrides["activated_by"] = None
    result = decide(campaign=b.active_campaign(**overrides))
    assert (result.decision, result.reasons) == (decision, (reason,))


def test_campaign_active_continues_but_respects_start_and_end() -> None:
    assert decide(campaign=b.active_campaign()).decision is OutboundDecision.SEND
    future = decide(campaign=b.active_campaign(start_at=b.T0 + timedelta(days=1)))
    assert future.reasons == (PolicyReason.CAMPAIGN_NOT_ACTIVE,)
    over = decide(campaign=b.active_campaign(start_at=b.T0 - timedelta(days=9), end_at=b.T0))
    assert (over.decision, over.reasons) == (OutboundDecision.SKIP, (PolicyReason.CAMPAIGN_ENDED,))


# ---- G. Precedence and reason preservation ----------------------------------------------


def test_suppression_and_bounce_skip() -> None:
    dnc = decide(suppression_entries=(f.dnc_entry(),))
    assert (dnc.decision, dnc.reasons) == (OutboundDecision.SKIP, (PolicyReason.DNC_EMAIL,))
    domain = decide(suppression_entries=(f.dnc_entry(scope=DNCScope.DOMAIN, value="prospect.example"),))
    assert domain.reasons == (PolicyReason.DNC_DOMAIN,)
    bounced = decide(contact=f.contact(email_validity=EmailValidity.BOUNCED))
    assert bounced.reasons == (PolicyReason.INVALID_OR_BOUNCED_ADDRESS,)


def test_skip_beats_hold_and_all_reasons_are_preserved_in_order() -> None:
    result = decide(
        now=SATURDAY,
        quota=quota(now=SATURDAY),
        suppression_entries=(f.dnc_entry(),),
        kill_switch=b.kill_switch(enabled=True),
        campaign=b.active_campaign(status=CampaignStatus.PAUSED),
    )
    assert result.decision is OutboundDecision.SKIP
    assert result.reasons == (
        PolicyReason.DNC_EMAIL,
        PolicyReason.CAMPAIGN_PAUSED,
        PolicyReason.KILL_SWITCH,
        PolicyReason.OUTSIDE_SENDING_WINDOW,
    )


def test_hold_beats_send() -> None:
    result = decide(now=SATURDAY, quota=quota(now=SATURDAY))
    assert (result.decision, result.reasons) == (OutboundDecision.HOLD, (PolicyReason.OUTSIDE_SENDING_WINDOW,))


def test_quota_exhaustion_holds() -> None:
    result = decide(limits=b.limits(sends=1), quota=quota([b.entry("o1")]))
    assert (result.decision, result.reasons) == (OutboundDecision.HOLD, (PolicyReason.GLOBAL_DAILY_LIMIT,))


def test_stage_3_never_escalates() -> None:
    worst = decide(
        now=SATURDAY,
        quota=quota(now=SATURDAY),
        suppression_entries=(f.dnc_entry(),),
        kill_switch=b.kill_switch(enabled=True),
    )
    assert OutboundDecision.ESCALATE not in {check.decision for check in worst.checks}


# ---- H. Follow-up policy ----------------------------------------------------------------


def follow_up(**overrides: object) -> PolicyDecisionResult:
    campaign = b.active_campaign(max_follow_ups=2, min_interval_between_follow_ups=timedelta(days=3))
    base: dict[str, object] = {
        "plan": f.follow_up_plan(max_steps=2, steps_sent=0),
        "campaign": campaign,
        "lead": f.lead(stage=LeadStage.CONTACTED),
        "contact": f.contact(),
        "last_outbound_at": b.T0 - timedelta(days=4),
        "now": b.T0,
        "limits": b.limits(per_contact=3, interval=timedelta(days=2)),
        "window": b.window(),
        "suppression_entries": (),
        "kill_switch": b.kill_switch(),
        "quota_snapshot": quota(),
    }
    return evaluate_follow_up_policy(**(base | overrides))  # type: ignore[arg-type]


def test_follow_up_send_case() -> None:
    result = follow_up()
    assert (result.decision, result.checks) == (OutboundDecision.SEND, ())


def test_follow_up_interval_not_reached_holds() -> None:
    # Campaign interval (3 days) is stricter than the global minimum (2 days).
    result = follow_up(last_outbound_at=b.T0 - timedelta(days=2, hours=23))
    assert (result.decision, result.reasons) == (OutboundDecision.HOLD, (PolicyReason.FOLLOWUP_INTERVAL,))
    assert follow_up(last_outbound_at=b.T0 - timedelta(days=3)).decision is OutboundDecision.SEND


def test_follow_up_max_per_contact_skips() -> None:
    by_plan = follow_up(plan=f.follow_up_plan(max_steps=2, steps_sent=2))
    assert (by_plan.decision, by_plan.reasons) == (OutboundDecision.SKIP, (PolicyReason.CONTACT_FOLLOWUP_LIMIT,))
    ledger = [b.entry(f"fu{i}", kind=OutboundKind.FOLLOW_UP, sending_at=b.T0 - timedelta(days=9 - i)) for i in range(2)]
    by_ledger = follow_up(quota_snapshot=quota(ledger))
    assert by_ledger.decision is OutboundDecision.SKIP
    assert PolicyReason.CONTACT_FOLLOWUP_LIMIT in by_ledger.reasons


def test_follow_up_dnc_skips() -> None:
    result = follow_up(suppression_entries=(f.dnc_entry(),))
    assert (result.decision, result.reasons) == (OutboundDecision.SKIP, (PolicyReason.DNC_EMAIL,))


def test_follow_up_paused_campaign_holds() -> None:
    campaign = b.active_campaign(status=CampaignStatus.PAUSED, max_follow_ups=2)
    result = follow_up(campaign=campaign)
    assert (result.decision, result.reasons) == (OutboundDecision.HOLD, (PolicyReason.CAMPAIGN_PAUSED,))


@pytest.mark.parametrize(
    ("overrides", "decision", "reason"),
    [
        ({"plan": f.follow_up_plan(status=FollowUpStatus.PAUSED)}, OutboundDecision.HOLD, PolicyReason.FOLLOWUP_PLAN_PAUSED),
        (
            {
                "plan": f.follow_up_plan(
                    status=FollowUpStatus.CANCELLED, cancel_reason=FollowUpCancelReason.REPLY_RECEIVED, next_due_at=None
                )
            },
            OutboundDecision.SKIP,
            PolicyReason.FOLLOWUP_PLAN_INACTIVE,
        ),
        ({"lead": f.lead(stage=LeadStage.ENGAGED)}, OutboundDecision.SKIP, PolicyReason.LEAD_NOT_AWAITING_REPLY),
        (
            {"lead": f.lead(stage=LeadStage.CONTACTED, status=LeadStatus.ON_HOLD)},
            OutboundDecision.HOLD,
            PolicyReason.LEAD_ON_HOLD,
        ),
        (
            {"lead": f.lead(stage=LeadStage.CONTACTED, status=LeadStatus.OPERATOR_OWNED)},
            OutboundDecision.SKIP,
            PolicyReason.LEAD_OPERATOR_OWNED,
        ),
        ({"kill_switch": b.kill_switch(enabled=True)}, OutboundDecision.HOLD, PolicyReason.KILL_SWITCH),
        ({"now": SATURDAY, "quota_snapshot": quota(now=SATURDAY)}, OutboundDecision.HOLD, PolicyReason.OUTSIDE_SENDING_WINDOW),
    ],
)
def test_follow_up_plan_lead_and_shared_checks(
    overrides: dict[str, object], decision: OutboundDecision, reason: PolicyReason
) -> None:
    result = follow_up(**overrides)
    assert (result.decision, result.reasons) == (decision, (reason,))


def test_follow_up_daily_quota_holds() -> None:
    result = follow_up(
        limits=b.limits(follow_ups=1, per_contact=3, interval=timedelta(days=2)),
        quota_snapshot=quota([b.entry("x", kind=OutboundKind.FOLLOW_UP, contact_id="contact-2")]),
    )
    assert (result.decision, result.reasons) == (OutboundDecision.HOLD, (PolicyReason.FOLLOWUP_DAILY_LIMIT,))


def test_follow_up_rejects_mismatched_inputs() -> None:
    with pytest.raises(ValueError):
        follow_up(lead=f.lead(lead_id="other", stage=LeadStage.CONTACTED))
    with pytest.raises(ValueError):
        follow_up(last_outbound_at=datetime(2026, 1, 1))
