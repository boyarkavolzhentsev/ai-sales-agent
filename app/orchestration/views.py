"""Read models over plans: the canonical lead view, queue membership, ordering, metrics.
Pure functions: every queue and count is derived from the canonical plan."""

from collections import Counter
from collections.abc import Iterable

from app.core.enums import LeadStage
from app.orchestration.models import (
    ExecutionAction,
    ExecutionMetrics,
    ExecutionOwner,
    ExecutionQueue,
    ExecutionSubsystem,
    SalesExecutionPlan,
    SalesExecutionView,
)
from app.orchestration.snapshot import LeadSnapshot
from app.pipeline.qualification import status_of

Q = ExecutionQueue
NO_ACTIONS = frozenset({ExecutionAction.NO_ACTION, ExecutionAction.NO_AUTOMATION})


def view_of(snapshot: LeadSnapshot, plan: SalesExecutionPlan) -> SalesExecutionView:
    p = snapshot.pipeline
    lead, conversation, member, opportunity = p.lead, p.conversation, p.member, p.opportunity
    job = snapshot.follow_up.open_job if snapshot.follow_up else None
    current = snapshot.commercial.current if snapshot.commercial else None
    return SalesExecutionView(
        lead_id=lead.lead_id, contact_id=lead.contact_id, lead_stage=lead.stage, lead_status=lead.status,
        lead_close_reason=lead.close_reason, last_intent=lead.last_intent, suppressed=p.suppressed,
        campaign_id=member.campaign_id if member else None, campaign_status=snapshot.campaign.status if snapshot.campaign else None,
        campaign_member_status=member.status if member else None,
        conversation_id=conversation.conversation_id if conversation else None,
        conversation_status=conversation.status if conversation else None,
        follow_up_status=job.status if job else None, follow_up_due_at=job.due_at if job else None,
        unresolved_outbound_ids=snapshot.unresolved_outbound_ids,
        pending_review_outbound_ids=tuple(m.outbound_id for m in snapshot.pending_review),
        approved_outbound_ids=tuple(m.outbound_id for m in snapshot.approved),
        open_escalation_ids=tuple(e.escalation_id for e in snapshot.escalations),
        qualification_status=status_of(p.qualification),
        opportunity_id=opportunity.opportunity_id if opportunity else None,
        opportunity_status=opportunity.status if opportunity else None,
        commercial_stage=snapshot.commercial_stage, revision_status=current.status if current else None,
        plan=plan, last_activity_at=plan.last_activity_at,
    )


def in_queue(which: ExecutionQueue, plan: SalesExecutionPlan) -> bool:
    if which is Q.ACTIONABLE:
        return plan.executable
    if which is Q.OPERATOR:
        return plan.requires_operator
    if which is Q.CUSTOMER:
        return plan.requires_customer
    if which is Q.RECOVERY:
        return plan.owner is ExecutionOwner.DISPATCH_RECOVERY
    if which is Q.BLOCKED:
        return is_blocked(plan)
    if which is Q.COMMERCIAL:
        return plan.subsystem is ExecutionSubsystem.COMMERCIAL
    if which is Q.CONVERSATION:
        return plan.owner is ExecutionOwner.CONVERSATION
    if which is Q.CAMPAIGN:
        return plan.owner is ExecutionOwner.CAMPAIGN
    return plan.owner is ExecutionOwner.NONE  # UNOWNED


def is_blocked(plan: SalesExecutionPlan) -> bool:
    """Neither executable nor waiting on a person, and something blocks it (DNC, kill
    switch, a missing capability, a policy refusal, a lease, not due yet, ...)."""
    return not plan.executable and not plan.requires_operator and not plan.requires_customer and bool(plan.blockers)


def order_key(plan: SalesExecutionPlan) -> tuple[int, str, str]:
    """Priority first, then the oldest activity (no starvation of old leads), then the lead
    id: fully deterministic, never random."""
    return plan.priority, plan.last_activity_at.isoformat(), plan.lead_id


def ordered(plans: Iterable[SalesExecutionPlan]) -> list[SalesExecutionPlan]:
    return sorted(plans, key=order_key)


def metrics_of(plans: Iterable[SalesExecutionPlan], by_stage: dict[LeadStage, int]) -> ExecutionMetrics:
    found = list(plans)
    return ExecutionMetrics(
        open_leads=len(found), closed_leads=by_stage.get(LeadStage.CLOSED, 0),
        by_owner=dict(sorted(Counter(p.owner for p in found).items())),
        by_action=dict(sorted(Counter(p.action for p in found).items())),
        actionable=sum(1 for p in found if p.executable),
        waiting_operator=sum(1 for p in found if p.requires_operator),
        waiting_customer=sum(1 for p in found if p.requires_customer),
        blocked=sum(1 for p in found if is_blocked(p)),
        recovery_required=sum(1 for p in found if p.owner is ExecutionOwner.DISPATCH_RECOVERY),
        no_action=sum(1 for p in found if p.action in NO_ACTIONS),
    )
