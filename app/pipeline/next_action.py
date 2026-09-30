"""Who owns the next action, and what blocks progress: a deterministic read model derived
from durable state (nothing here is stored, scheduled or executed).

``gather`` reads the facts in one transaction; ``derive`` is pure. The first matching rule
decides the owner and action; ``blockers`` lists every applicable reason, in a fixed order.
"""

from dataclasses import dataclass
from datetime import datetime

from app.core.enums import (
    BlockerCode,
    CampaignMemberStatus,
    ConversationStatus,
    EscalationStatus,
    LeadIntent,
    LeadStage,
    LeadStatus,
    NextActionOwner,
    NextActionType,
    OpportunityStatus,
    OutboundStatus,
    QualificationStatus,
)
from app.core.models import TERMINAL_CONVERSATION_STATUSES, CampaignMember, Conversation, Lead, LeadQualification, Opportunity
from app.core.models.base import CoreModel
from app.persistence import UnitOfWork
from app.pipeline.config import QualificationProfile
from app.pipeline.guards import is_suppressed
from app.pipeline.policy import OPERATOR_STAGES
from app.pipeline.qualification import QualificationGap, gaps

IN_SEQUENCE = frozenset({CampaignMemberStatus.ENROLLED, CampaignMemberStatus.DRAFTED, CampaignMemberStatus.APPROVED,
                         CampaignMemberStatus.DISPATCHING, CampaignMemberStatus.WAITING})
REVIEW_STATUSES = frozenset({OutboundStatus.DRAFTED, OutboundStatus.PENDING_REVIEW})
OPEN_ESCALATIONS = frozenset({EscalationStatus.OPEN, EscalationStatus.ACKNOWLEDGED})


class NextAction(CoreModel):
    owner: NextActionOwner
    action: NextActionType
    blockers: tuple[BlockerCode, ...] = ()


@dataclass(frozen=True)
class PipelineFacts:
    lead: Lead
    suppressed: bool
    qualification: LeadQualification | None
    gaps: tuple[QualificationGap, ...]
    opportunity: Opportunity | None
    conversation: Conversation | None  # the most recently active one
    member: CampaignMember | None
    unresolved_dispatch: bool
    open_escalation: bool
    pending_review: bool
    approved_undispatched: bool


def gather(uow: UnitOfWork, lead: Lead, profile: QualificationProfile, now: datetime) -> PipelineFacts:
    qualification = uow.qualifications.get(lead.lead_id)
    conversations = uow.conversations.list_by_lead(lead.lead_id)
    outbound = uow.outbound.list_by_lead(lead.lead_id)
    return PipelineFacts(
        lead=lead,
        suppressed=is_suppressed(uow, lead, now),
        qualification=qualification,
        gaps=gaps(profile, qualification) if qualification is not None or lead.stage is LeadStage.QUALIFYING else (),
        opportunity=uow.opportunities.get_active_for_lead(lead.lead_id),
        conversation=max(conversations, key=lambda c: (c.last_activity_at, c.conversation_id), default=None),
        member=uow.campaign_members.get_by_lead(lead.lead_id),
        unresolved_dispatch=any(m.status is OutboundStatus.SENDING for m in outbound),
        open_escalation=any(e.status in OPEN_ESCALATIONS for e in uow.escalations.list_by_lead(lead.lead_id)),
        pending_review=any(m.status in REVIEW_STATUSES for m in outbound),
        approved_undispatched=any(m.status is OutboundStatus.OPERATOR_APPROVED for m in outbound),
    )


def blockers(f: PipelineFacts) -> tuple[BlockerCode, ...]:
    lead, conversation = f.lead, f.conversation
    found: list[BlockerCode] = []
    if f.suppressed:
        found.append(BlockerCode.DNC)
    if lead.stage is LeadStage.CLOSED:
        found.append(BlockerCode.CLOSED_LEAD)
    if f.unresolved_dispatch:
        found.append(BlockerCode.UNRESOLVED_DISPATCH)
    if f.open_escalation or f.pending_review or (conversation and conversation.status is ConversationStatus.OPERATOR_REVIEW):
        found.append(BlockerCode.OPERATOR_REVIEW)
    if lead.status is LeadStatus.ON_HOLD:
        found.append(BlockerCode.LEAD_ON_HOLD)
    if f.qualification is not None and f.qualification.open_conflicts:
        found.append(BlockerCode.QUALIFICATION_CONFLICT)
    if any(g.reason == "MISSING_REQUIRED" for g in f.gaps) and lead.stage is not LeadStage.CLOSED:
        found.append(BlockerCode.MISSING_QUALIFICATION)
    if _declined(f):
        found.append(BlockerCode.CUSTOMER_DECLINED)
    if f.member is not None and f.member.status in IN_SEQUENCE:
        found.append(BlockerCode.CAMPAIGN_OWNS_PRE_REPLY)
    if conversation is not None and conversation.status is ConversationStatus.WAITING_FOR_REPLY:
        found.append(BlockerCode.ACTIVE_CUSTOMER_WAIT)
    if conversation is not None and conversation.status is ConversationStatus.PAUSED:
        found.append(BlockerCode.CONVERSATION_PAUSED)
    if _no_active_conversation(f):
        found.append(BlockerCode.NO_ACTIVE_CONVERSATION)
    return tuple(found)


def derive(f: PipelineFacts) -> NextAction:
    found = blockers(f)
    owner, action = _decide(f)
    return NextAction(owner=owner, action=action, blockers=found)


def _decide(f: PipelineFacts) -> tuple[NextActionOwner, NextActionType]:
    lead, conversation, qualification = f.lead, f.conversation, f.qualification
    status = conversation.status if conversation is not None else None
    if f.suppressed:
        return NextActionOwner.NONE, NextActionType.NONE  # DNC wins over everything
    if lead.stage is LeadStage.CLOSED:
        return NextActionOwner.NONE, NextActionType.CLOSED
    if f.unresolved_dispatch:
        return NextActionOwner.AGENT, NextActionType.AWAIT_DISPATCH_RESOLUTION
    if f.open_escalation or f.pending_review or status is ConversationStatus.OPERATOR_REVIEW or lead.status is LeadStatus.ON_HOLD:
        return NextActionOwner.OPERATOR, NextActionType.OPERATOR_REVIEW
    if qualification is not None and qualification.open_conflicts:
        return NextActionOwner.OPERATOR, NextActionType.RESOLVE_QUALIFICATION_CONFLICT
    if qualification is not None and qualification.status is QualificationStatus.READY_FOR_REVIEW:
        return NextActionOwner.OPERATOR, NextActionType.REVIEW_QUALIFICATION
    if _declined(f):
        return NextActionOwner.OPERATOR, NextActionType.OPERATOR_DECISION
    if f.approved_undispatched:
        return NextActionOwner.AGENT, NextActionType.RESPOND  # an approved reply awaits dispatch
    if lead.stage is LeadStage.NEGOTIATION:
        return NextActionOwner.OPERATOR, NextActionType.OPERATOR_DECISION
    if lead.stage is LeadStage.OPPORTUNITY or (f.opportunity and f.opportunity.status is OpportunityStatus.OPEN):
        return NextActionOwner.OPERATOR, NextActionType.PREPARE_PROPOSAL
    if lead.stage is LeadStage.QUALIFIED:
        return NextActionOwner.OPERATOR, NextActionType.DECIDE_OPPORTUNITY
    if f.member is not None and f.member.status in IN_SEQUENCE:
        return NextActionOwner.AGENT, NextActionType.CAMPAIGN_OUTREACH
    if status is ConversationStatus.WAITING_FOR_REPLY:
        return NextActionOwner.CUSTOMER, NextActionType.WAIT_FOR_REPLY
    if status is ConversationStatus.FOLLOW_UP_DUE:
        return NextActionOwner.AGENT, NextActionType.FOLLOW_UP
    if status is ConversationStatus.PAUSED or _no_active_conversation(f):
        return NextActionOwner.OPERATOR, NextActionType.OPERATOR_DECISION
    if status is ConversationStatus.ACTIVE:
        if any(g.reason == "MISSING_REQUIRED" and g.safe_to_ask for g in f.gaps):
            return NextActionOwner.AGENT, NextActionType.QUALIFY
        return NextActionOwner.AGENT, NextActionType.RESPOND
    return NextActionOwner.OPERATOR, NextActionType.OPERATOR_DECISION


def _declined(f: PipelineFacts) -> bool:
    """The customer declined while the lead is in an operator stage, where no automatic
    close applies: an operator decides (LOST or continue)."""
    return f.lead.stage in OPERATOR_STAGES and f.lead.last_intent is LeadIntent.NOT_INTERESTED


def _no_active_conversation(f: PipelineFacts) -> bool:
    engaged = f.lead.stage not in (LeadStage.NEW, LeadStage.CONTACTED, LeadStage.CLOSED)
    return engaged and (f.conversation is None or f.conversation.status in TERMINAL_CONVERSATION_STATUSES)
