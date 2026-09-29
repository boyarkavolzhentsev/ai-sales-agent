"""Pure follow-up eligibility. Does not schedule, create or modify anything."""

from datetime import datetime

from app.core.enums import FollowUpStatus, LeadStage, LeadStatus, OutboundKind
from app.core.models import Campaign, DoNotContactEntry, FollowUpPlan, Lead, ProspectContact
from app.policy.limits import LimitPolicy, effective_max_follow_ups, effective_min_interval
from app.policy.models import KillSwitchState, PolicyCheck, PolicyDecisionResult, PolicyReason
from app.policy.outbound import PolicyContext, outbound_checks
from app.policy.quota import QuotaSnapshot
from app.policy.windows import SendingWindow


def evaluate_follow_up_policy(
    *,
    plan: FollowUpPlan,
    campaign: Campaign,
    lead: Lead,
    contact: ProspectContact,
    last_outbound_at: datetime,
    now: datetime,
    limits: LimitPolicy,
    window: SendingWindow,
    suppression_entries: tuple[DoNotContactEntry, ...],
    kill_switch: KillSwitchState,
    quota_snapshot: QuotaSnapshot,
    company_domain: str | None = None,
) -> PolicyDecisionResult:
    """Whether the next follow-up of ``plan`` may be sent now.

    Plan/lead checks come first, then the shared outbound checks (DNC, bounce, campaign
    status, kill switch, sending window, quota including the per-contact cap).
    """
    if plan.lead_id != lead.lead_id or plan.campaign_id != campaign.campaign_id:
        raise ValueError("plan, lead and campaign do not belong together")
    if lead.contact_id != contact.contact_id:
        raise ValueError("lead and contact do not belong together")
    if last_outbound_at.tzinfo is None or last_outbound_at.utcoffset() is None:
        raise ValueError("last_outbound_at must be timezone-aware")

    checks: list[PolicyCheck] = []
    if plan.status is FollowUpStatus.PAUSED:
        checks.append(PolicyCheck(reason=PolicyReason.FOLLOWUP_PLAN_PAUSED, detail="plan paused"))
    elif plan.status is not FollowUpStatus.ACTIVE:
        checks.append(PolicyCheck(reason=PolicyReason.FOLLOWUP_PLAN_INACTIVE, detail=f"plan {plan.status}"))

    if lead.stage is not LeadStage.CONTACTED:
        checks.append(PolicyCheck(reason=PolicyReason.LEAD_NOT_AWAITING_REPLY, detail=f"lead stage {lead.stage}"))
    if lead.status is LeadStatus.OPERATOR_OWNED:
        checks.append(PolicyCheck(reason=PolicyReason.LEAD_OPERATOR_OWNED, detail="lead taken over by operator"))
    elif lead.status is LeadStatus.ON_HOLD:
        checks.append(PolicyCheck(reason=PolicyReason.LEAD_ON_HOLD, detail="lead on hold"))

    cap = min(effective_max_follow_ups(limits, campaign), plan.max_steps)
    if plan.steps_sent >= cap:
        checks.append(
            PolicyCheck(reason=PolicyReason.CONTACT_FOLLOWUP_LIMIT, detail=f"plan steps {plan.steps_sent}/{cap}")
        )

    interval = effective_min_interval(limits, campaign)
    if now - last_outbound_at < interval:
        checks.append(
            PolicyCheck(
                reason=PolicyReason.FOLLOWUP_INTERVAL,
                detail=f"{now - last_outbound_at} since last message; minimum {interval}",
            )
        )

    context = PolicyContext(
        now=now,
        kind=OutboundKind.FOLLOW_UP,
        contact=contact,
        company_domain=company_domain,
        campaign=campaign,
        suppression_entries=suppression_entries,
        kill_switch=kill_switch,
        window=window,
        limits=limits,
        quota=quota_snapshot,
    )
    checks.extend(outbound_checks(context))
    return PolicyDecisionResult.from_checks(checks)
