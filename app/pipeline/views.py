"""Operator-facing pipeline read models, queues and metrics (no message bodies)."""

from datetime import datetime
from enum import StrEnum

from pydantic import AwareDatetime

from app.core.enums import (
    CampaignMemberStatus,
    CloseReason,
    ConversationStatus,
    LeadStage,
    LeadStatus,
    NextActionOwner,
    OpportunityStatus,
    QualificationStatus,
)
from app.core.models.base import CoreModel
from app.core.models.types import EntityId
from app.persistence import UnitOfWork
from app.pipeline.config import QualificationProfile
from app.pipeline.next_action import NextAction, PipelineFacts, derive, gather
from app.pipeline.policy import OPEN_STAGES, PIPELINE_TRANSITION
from app.pipeline.qualification import QualificationGap, status_of


class LeadPipelineView(CoreModel):
    lead_id: EntityId
    stage: LeadStage
    status: LeadStatus
    close_reason: CloseReason | None = None  # the terminal reason when CLOSED
    lead_version: int
    qualification_status: QualificationStatus
    qualification_version: int | None = None
    open_conflict_ids: tuple[EntityId, ...] = ()
    gaps: tuple[QualificationGap, ...] = ()
    opportunity_id: EntityId | None = None
    opportunity_status: OpportunityStatus | None = None
    opportunity_version: int | None = None
    conversation_status: ConversationStatus | None = None
    campaign_member_status: CampaignMemberStatus | None = None
    suppressed: bool
    next_action: NextAction
    last_activity_at: AwareDatetime


class PipelineQueue(StrEnum):
    NEEDS_OPERATOR = "NEEDS_OPERATOR"
    NEEDS_QUALIFICATION = "NEEDS_QUALIFICATION"
    OPEN_OPPORTUNITIES = "OPEN_OPPORTUNITIES"
    WAITING_ON_CUSTOMER = "WAITING_ON_CUSTOMER"
    BLOCKED = "BLOCKED"
    RECENTLY_CLOSED = "RECENTLY_CLOSED"


class TransitionCount(CoreModel):
    from_stage: str
    to_stage: str
    trigger: str
    count: int


class PipelineMetrics(CoreModel):
    """From current durable state. Transition counts cover only transitions recorded by the
    Stage 12 policy (earlier history is not reconstructed); no stage ages are reported
    because entry timestamps were never recorded for older transitions."""

    leads_by_stage: dict[LeadStage, int]
    closed_by_reason: dict[CloseReason, int]
    qualification_by_status: dict[QualificationStatus, int]
    opportunities_by_status: dict[OpportunityStatus, int]
    won: int
    lost: int
    leads_requiring_operator: int
    transitions: tuple[TransitionCount, ...]


def view_of(facts: PipelineFacts) -> LeadPipelineView:
    lead, q, opp, conv = facts.lead, facts.qualification, facts.opportunity, facts.conversation
    activity = [lead.updated_at, *([conv.last_activity_at] if conv else [])]
    return LeadPipelineView(
        lead_id=lead.lead_id, stage=lead.stage, status=lead.status, close_reason=lead.close_reason,
        lead_version=lead.version, qualification_status=status_of(q), qualification_version=q.version if q else None,
        open_conflict_ids=tuple(c.conflict_id for c in q.open_conflicts) if q else (), gaps=facts.gaps,
        opportunity_id=opp.opportunity_id if opp else None, opportunity_status=opp.status if opp else None,
        opportunity_version=opp.version if opp else None, conversation_status=conv.status if conv else None,
        campaign_member_status=facts.member.status if facts.member else None, suppressed=facts.suppressed,
        next_action=derive(facts), last_activity_at=max(activity),
    )


def lead_view(uow: UnitOfWork, lead_id: str, profile: QualificationProfile, now: datetime) -> LeadPipelineView | None:
    lead = uow.leads.get(lead_id)
    return None if lead is None else view_of(gather(uow, lead, profile, now))


def queue(uow: UnitOfWork, which: PipelineQueue, profile: QualificationProfile, now: datetime,
          limit: int) -> tuple[LeadPipelineView, ...]:
    if which is PipelineQueue.RECENTLY_CLOSED:
        closed = uow.leads.list_by_stages([LeadStage.CLOSED], limit)
        return tuple(view_of(gather(uow, lead, profile, now)) for lead in closed
                     if lead.close_reason in (CloseReason.WON, CloseReason.LOST, CloseReason.DISQUALIFIED))
    views = [view_of(gather(uow, lead, profile, now)) for lead in uow.leads.list_by_stages(OPEN_STAGES, limit)]
    selected = [v for v in views if _matches(which, v)]
    return tuple(sorted(selected, key=lambda v: (v.last_activity_at, v.lead_id)))


def _matches(which: PipelineQueue, view: LeadPipelineView) -> bool:
    if which is PipelineQueue.NEEDS_OPERATOR:
        return view.next_action.owner is NextActionOwner.OPERATOR
    if which is PipelineQueue.NEEDS_QUALIFICATION:
        return view.qualification_status is QualificationStatus.IN_PROGRESS and any(
            g.reason == "MISSING_REQUIRED" for g in view.gaps)
    if which is PipelineQueue.OPEN_OPPORTUNITIES:
        return view.opportunity_status in (OpportunityStatus.OPEN, OpportunityStatus.NEGOTIATING)
    if which is PipelineQueue.WAITING_ON_CUSTOMER:
        return view.next_action.owner is NextActionOwner.CUSTOMER
    return bool(view.next_action.blockers) and view.next_action.owner is not NextActionOwner.CUSTOMER  # BLOCKED


def metrics(uow: UnitOfWork, profile: QualificationProfile, now: datetime, limit: int) -> PipelineMetrics:
    by_stage = uow.leads.count_by_stage()
    closed = uow.leads.list_by_stages([LeadStage.CLOSED], 1_000_000)
    by_reason: dict[CloseReason, int] = {}
    for lead in closed:
        if lead.close_reason is not None:
            by_reason[lead.close_reason] = by_reason.get(lead.close_reason, 0) + 1
    counts: dict[tuple[str, str, str], int] = {}
    for event in uow.audit.list_by_event_type(PIPELINE_TRANSITION, 1_000_000):
        before, after = event.before or {}, event.after or {}
        key = (str(before.get("stage")), str(after.get("stage")), str(after.get("trigger")))
        counts[key] = counts.get(key, 0) + 1
    operator_queue = queue(uow, PipelineQueue.NEEDS_OPERATOR, profile, now, limit)
    return PipelineMetrics(
        leads_by_stage=by_stage, closed_by_reason=by_reason,
        qualification_by_status=uow.qualifications.count_by_status(),
        opportunities_by_status=uow.opportunities.count_by_status(),
        won=by_reason.get(CloseReason.WON, 0), lost=by_reason.get(CloseReason.LOST, 0),
        leads_requiring_operator=len(operator_queue),
        transitions=tuple(TransitionCount(from_stage=f, to_stage=t, trigger=g, count=n)
                          for (f, t, g), n in sorted(counts.items())),
    )
