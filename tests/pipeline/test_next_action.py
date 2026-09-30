"""Next-action ownership and blockers: deterministic derivation from durable facts."""

from datetime import timedelta

import pytest

from app.core.enums import (
    BlockerCode,
    CampaignMemberStatus,
    CloseReason,
    ConfidenceBand,
    ConflictStatus,
    ConversationStatus,
    FactSource,
    LeadIntent,
    LeadOrigin,
    LeadStage,
    LeadStatus,
    NextActionOwner,
    NextActionType,
    OpportunityStatus,
    QualificationStatus,
)
from app.core.models import (
    CampaignMember,
    Conversation,
    FactEvidence,
    Lead,
    LeadQualification,
    Opportunity,
    QualificationConflict,
    QualificationFact,
)
from app.pipeline.config import GENERIC_PROFILE
from app.pipeline.next_action import PipelineFacts, derive
from app.pipeline.qualification import gaps
from tests.inbound.builders import NOW

O, A, B = NextActionOwner, NextActionType, BlockerCode
LATER = NOW + timedelta(minutes=5)
EVIDENCE = FactEvidence(source=FactSource.EXTRACTION, message_id="em-1", recorded_at=NOW)


def lead(stage: LeadStage = LeadStage.ENGAGED, **fields: object) -> Lead:
    return Lead.model_validate({"lead_id": "ld-1", "contact_id": "ct-1", "origin": LeadOrigin.INBOUND, "stage": stage,
                                "created_at": NOW, "updated_at": LATER} | fields)


def conversation(status: ConversationStatus) -> Conversation:
    extra = {"next_follow_up_at": LATER} if status is ConversationStatus.FOLLOW_UP_DUE else {}
    return Conversation.model_validate({"conversation_id": "cv-1", "thread_id": "th-1", "lead_id": "ld-1", "contact_id": "ct-1",
                                        "status": status, "last_activity_at": NOW, "created_at": NOW, "updated_at": NOW} | extra)


def qualification(status: QualificationStatus = QualificationStatus.IN_PROGRESS, *, facts: tuple[str, ...] = (),
                  conflict: bool = False) -> LeadQualification:
    conflicts = (QualificationConflict(conflict_id="qc-1", field="budget", current_value="50k", proposed_value="20k",
                                       evidence=EVIDENCE, status=ConflictStatus.OPEN),) if conflict else ()
    decided = {"decided_by": "op", "decided_at": NOW} if status is QualificationStatus.QUALIFIED else {}
    return LeadQualification.model_validate({
        "lead_id": "ld-1", "profile_id": "generic-v1", "status": status, "conflicts": conflicts,
        "facts": [QualificationFact(field=f, value="x", confidence=ConfidenceBand.HIGH, evidence=(EVIDENCE,)) for f in facts],
        "created_at": NOW, "updated_at": NOW} | decided)


def facts(*, lead_: Lead | None = None, conv: ConversationStatus | None = ConversationStatus.ACTIVE,
          q: LeadQualification | None = None, opportunity: OpportunityStatus | None = None,
          member: CampaignMemberStatus | None = None, **flags: bool) -> PipelineFacts:
    the_lead = lead_ or lead()
    opp = None
    if opportunity is not None:
        opp = Opportunity(opportunity_id="op-1", lead_id="ld-1", status=opportunity, owner_operator_id="op",
                          created_at=NOW, updated_at=NOW)
    campaign_member = None
    if member is not None:
        campaign_member = CampaignMember.model_validate({"member_id": "cm-1", "campaign_id": "camp-1", "contact_id": "ct-1",
                                                         "lead_id": "ld-1", "status": member, "enrolled_at": NOW, "last_activity_at": NOW,
                                                         "created_at": NOW, "updated_at": NOW})
    return PipelineFacts(
        lead=the_lead, suppressed=flags.get("suppressed", False), qualification=q,
        gaps=gaps(GENERIC_PROFILE, q) if q is not None else (), opportunity=opp,
        conversation=conversation(conv) if conv is not None else None, member=campaign_member,
        unresolved_dispatch=flags.get("unresolved_dispatch", False), open_escalation=flags.get("open_escalation", False),
        pending_review=flags.get("pending_review", False), approved_undispatched=flags.get("approved_undispatched", False),
    )


@pytest.mark.parametrize(
    ("case", "owner", "action", "blocker"),
    [
        (facts(conv=ConversationStatus.WAITING_FOR_REPLY), O.CUSTOMER, A.WAIT_FOR_REPLY, B.ACTIVE_CUSTOMER_WAIT),
        (facts(q=qualification(facts=("need",))), O.AGENT, A.QUALIFY, B.MISSING_QUALIFICATION),
        (facts(q=qualification(QualificationStatus.READY_FOR_REVIEW, facts=("need", "product_interest", "timeframe",
                                                                             "decision_role"))),
         O.OPERATOR, A.REVIEW_QUALIFICATION, None),
        (facts(q=qualification(facts=("need",), conflict=True)), O.OPERATOR, A.RESOLVE_QUALIFICATION_CONFLICT,
         B.QUALIFICATION_CONFLICT),
        (facts(lead_=lead(LeadStage.QUALIFIED), conv=ConversationStatus.WAITING_FOR_REPLY), O.OPERATOR, A.DECIDE_OPPORTUNITY, None),
        (facts(lead_=lead(LeadStage.OPPORTUNITY), opportunity=OpportunityStatus.OPEN), O.OPERATOR, A.PREPARE_PROPOSAL, None),
        (facts(lead_=lead(LeadStage.NEGOTIATION), opportunity=OpportunityStatus.NEGOTIATING), O.OPERATOR, A.OPERATOR_DECISION, None),
        (facts(suppressed=True, lead_=lead(LeadStage.OPPORTUNITY)), O.NONE, A.NONE, B.DNC),
        (facts(lead_=lead(LeadStage.CLOSED, close_reason=CloseReason.WON), conv=ConversationStatus.CONVERTED),
         O.NONE, A.CLOSED, B.CLOSED_LEAD),
        (facts(lead_=lead(LeadStage.CLOSED, close_reason=CloseReason.LOST), conv=ConversationStatus.CLOSED),
         O.NONE, A.CLOSED, B.CLOSED_LEAD),
        (facts(unresolved_dispatch=True, conv=ConversationStatus.ACTIVE), O.AGENT, A.AWAIT_DISPATCH_RESOLUTION,
         B.UNRESOLVED_DISPATCH),
        (facts(lead_=lead(LeadStage.CONTACTED), conv=None, member=CampaignMemberStatus.WAITING), O.AGENT, A.CAMPAIGN_OUTREACH,
         B.CAMPAIGN_OWNS_PRE_REPLY),
        (facts(pending_review=True), O.OPERATOR, A.OPERATOR_REVIEW, B.OPERATOR_REVIEW),
        (facts(open_escalation=True, conv=ConversationStatus.OPERATOR_REVIEW), O.OPERATOR, A.OPERATOR_REVIEW, B.OPERATOR_REVIEW),
        (facts(conv=ConversationStatus.FOLLOW_UP_DUE), O.AGENT, A.FOLLOW_UP, None),
        (facts(conv=ConversationStatus.PAUSED), O.OPERATOR, A.OPERATOR_DECISION, B.CONVERSATION_PAUSED),
        (facts(conv=ConversationStatus.CLOSED), O.OPERATOR, A.OPERATOR_DECISION, B.NO_ACTIVE_CONVERSATION),
        (facts(lead_=lead(LeadStage.QUALIFIED, last_intent=LeadIntent.NOT_INTERESTED)), O.OPERATOR, A.OPERATOR_DECISION,
         B.CUSTOMER_DECLINED),
        (facts(approved_undispatched=True), O.AGENT, A.RESPOND, None),
        (facts(lead_=lead(status=LeadStatus.ON_HOLD)), O.OPERATOR, A.OPERATOR_REVIEW, B.LEAD_ON_HOLD),
    ],
)
def test_next_action_derivation(case: PipelineFacts, owner: NextActionOwner, action: NextActionType,
                                blocker: BlockerCode | None) -> None:
    result = derive(case)
    assert (result.owner, result.action) == (owner, action)
    if blocker is not None:
        assert blocker in result.blockers
    assert derive(case) == result  # deterministic


def test_dnc_outranks_everything_else() -> None:
    worst = facts(suppressed=True, unresolved_dispatch=True, open_escalation=True, pending_review=True,
                  q=qualification(facts=("need",), conflict=True), lead_=lead(LeadStage.NEGOTIATION),
                  opportunity=OpportunityStatus.NEGOTIATING)
    result = derive(worst)
    assert (result.owner, result.action, result.blockers[0]) == (O.NONE, A.NONE, B.DNC)
    assert {B.UNRESOLVED_DISPATCH, B.OPERATOR_REVIEW, B.QUALIFICATION_CONFLICT} <= set(result.blockers)


def test_blocker_order_is_stable() -> None:
    result = derive(facts(pending_review=True, q=qualification(facts=("need",), conflict=True)))
    assert list(result.blockers) == sorted(result.blockers, key=list(BlockerCode).index)
