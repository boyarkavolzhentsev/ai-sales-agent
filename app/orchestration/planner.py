"""The pure execution planner: one snapshot in, exactly one owner and one action out.

Ownership precedence (first match wins; derived from the Stage 6-13 invariants):

  1. DNC (contact suppressed)          NONE / NO_AUTOMATION   (Stage 3/12/13: DNC wins over everything)
  2. lead CLOSED                       NONE / NO_ACTION       (terminal: nothing progresses)
  3. unresolved dispatch (contact)     DISPATCH_RECOVERY / RECONCILE_DISPATCH
                                       (Stage 8: UNKNOWN never means sent; no follow-up, campaign
                                       touch, approval-dependent step or second copy until resolved)
  4. open escalation                   OPERATOR / ESCALATION_REVIEW (automation on the lead is on hold)
  5. lead ON_HOLD                      OPERATOR / ESCALATION_REVIEW (Stage 7 blocks approvals on hold)
  6. draft awaiting review             OPERATOR / REVIEW_CAMPAIGN_DRAFT | REVIEW_REPLY_DRAFT
  7. conversation OPERATOR_REVIEW      OPERATOR / ESCALATION_REVIEW
  8. operator-approved message         CAMPAIGN | CONVERSATION / SEND_APPROVED_MESSAGE (Stage 8 gates)
                                       Deliberately ahead of Stage 12's own order (which lists it
                                       after qualification review): the approval is a decision an
                                       operator already took, and Stage 8 revalidation does not
                                       depend on qualification or commercial state, so those
                                       reviews come next instead of holding a reply back.
  9. qualification conflict / ready    OPERATOR / REVIEW_QUALIFICATION      (Stage 12 order)
 10. customer declined in operator band OPERATOR / DECIDE_LOSS              (no automatic close)
 11. active opportunity                Stage 13 next action (OPERATOR, or CUSTOMER waiting for a
                                       decision, yielding to a follow-up Stage 9 policy permits)
 12. lead QUALIFIED                    OPERATOR / CREATE_OPPORTUNITY
     OPPORTUNITY/NEGOTIATION stage without an active opportunity: OPERATOR / WAIT_FOR_OPERATOR
 13. campaign membership in sequence   CAMPAIGN / PREPARE_CAMPAIGN_TOUCH (or CUSTOMER while waiting)
                                       (Stage 10: pre-reply only; a reply ends it permanently)
 14. live conversation                 CONVERSATION / PROCESS_FOLLOW_UP when Stage 9 policy permits,
                                       else CUSTOMER / WAIT_FOR_CUSTOMER; ACTIVE (customer wrote
                                       last, nothing drafted) -> OPERATOR; PAUSED -> OPERATOR
 15. engaged lead, no conversation     OPERATOR / WAIT_FOR_OPERATOR
 16. otherwise                         NONE / NO_ACTION

Historical state never gives a second owner: a campaign membership counts only while it is
in sequence (REPLIED/COMPLETED/... are ignored), a conversation only while not terminal,
commercial state only for the active opportunity.

``executable`` is True only for an automatic action (AUTOMATIC_ACTIONS) with no blocker.
Kill switch, missing capabilities, leases and any refusal Stage 8's own claim gates would
give now (policy, Stage 7 gates, Stage 9/10 guards; SEND_BLOCKED_BY_POLICY with the source
codes) are explicit blockers, so a refused send is not retried by every pass.
"""

import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import datetime

from app.core.enums import (
    BlockerCode,
    CampaignJobStatus,
    CampaignMemberStatus,
    CampaignStatus,
    CommercialAction,
    CommercialBlocker,
    ConversationStatus,
    FollowUpJobStatus,
    LeadStage,
    LeadStatus,
    NextActionOwner,
    OutboundKind,
    QualificationStatus,
    SignalKind,
)
from app.core.models.base import CoreModel
from app.orchestration.models import (
    AUTOMATIC_ACTIONS,
    AUTOMATIC_OPERATIONS,
    OPERATOR_COMMANDS,
    OUTBOUND_ACTIONS,
    PRIORITY,
    EntityRefs,
    ExecutionAction,
    ExecutionBlocker,
    ExecutionCapabilities,
    ExecutionOwner,
    ExecutionSubsystem,
    SalesExecutionPlan,
)
from app.orchestration.snapshot import LeadSnapshot
from app.pipeline.next_action import IN_SEQUENCE

A, B, O, S = ExecutionAction, ExecutionBlocker, ExecutionOwner, ExecutionSubsystem
CAMPAIGN_KINDS = frozenset({OutboundKind.FIRST_TOUCH, OutboundKind.FOLLOW_UP})

PIPELINE_BLOCKERS: dict[BlockerCode, ExecutionBlocker] = {
    BlockerCode.DNC: B.DNC, BlockerCode.CLOSED_LEAD: B.LEAD_CLOSED, BlockerCode.UNRESOLVED_DISPATCH: B.UNRESOLVED_DISPATCH,
    BlockerCode.OPERATOR_REVIEW: B.OPERATOR_REVIEW_REQUIRED, BlockerCode.QUALIFICATION_CONFLICT: B.QUALIFICATION_CONFLICT,
    BlockerCode.MISSING_QUALIFICATION: B.QUALIFICATION_INCOMPLETE, BlockerCode.CUSTOMER_DECLINED: B.CUSTOMER_DECLINED,
    BlockerCode.ACTIVE_CUSTOMER_WAIT: B.WAITING_FOR_CUSTOMER, BlockerCode.CAMPAIGN_OWNS_PRE_REPLY: B.CAMPAIGN_PRE_REPLY_OWNERSHIP,
    BlockerCode.CONVERSATION_PAUSED: B.CONVERSATION_PAUSED, BlockerCode.NO_ACTIVE_CONVERSATION: B.NO_ACTIVE_CONVERSATION,
    BlockerCode.LEAD_ON_HOLD: B.LEAD_ON_HOLD,
}
CB = CommercialBlocker
COMMERCIAL_BLOCKERS: dict[CommercialBlocker, ExecutionBlocker] = {
    CB.DNC: B.DNC, CB.LEAD_CLOSED: B.LEAD_CLOSED, CB.OPPORTUNITY_NOT_OPEN: B.OPPORTUNITY_REQUIRED,
    CB.QUALIFICATION_NOT_APPROVED: B.QUALIFICATION_INCOMPLETE, CB.QUALIFICATION_CONFLICT: B.QUALIFICATION_CONFLICT,
    CB.NO_PROPOSAL: B.COMMERCIAL_INPUT_MISSING, CB.NO_PROPOSAL_LINES: B.COMMERCIAL_INPUT_MISSING,
    CB.MISSING_PRICE: B.COMMERCIAL_INPUT_MISSING, CB.MISSING_CURRENCY: B.COMMERCIAL_INPUT_MISSING,
    CB.CURRENCY_NOT_ALLOWED: B.COMMERCIAL_INPUT_MISSING, CB.MISSING_REQUIRED_TERM: B.COMMERCIAL_INPUT_MISSING,
    CB.DISCOUNT_NOT_ALLOWED: B.COMMERCIAL_INPUT_MISSING, CB.UNAPPROVED_TERM_REQUEST: B.TERM_REQUEST_OPEN,
    CB.OPEN_OBJECTION: B.OBJECTION_OPEN, CB.PROPOSAL_NOT_APPROVED: B.PROPOSAL_REVIEW_REQUIRED,
    CB.PROPOSAL_NOT_PRESENTED: B.PROPOSAL_NOT_PRESENTED, CB.PROPOSAL_REVISION_REQUIRED: B.PROPOSAL_REVISION_REQUIRED,
    CB.ACCEPTANCE_SIGNAL: B.ACCEPTANCE_REQUIRES_OPERATOR, CB.DECLINE_SIGNAL: B.DECLINE_REQUIRES_OPERATOR,
}
CA = CommercialAction
COMMERCIAL_ACTIONS: dict[CommercialAction, ExecutionAction] = {
    CA.COMPLETE_COMMERCIAL_INPUTS: A.PREPARE_PROPOSAL, CA.PREPARE_PROPOSAL: A.PREPARE_PROPOSAL,
    CA.REVIEW_TERM_REQUEST: A.REVIEW_TERM_REQUEST, CA.REVIEW_PROPOSAL: A.REVIEW_PROPOSAL,
    CA.PRESENT_PROPOSAL: A.PRESENT_PROPOSAL, CA.WAIT_FOR_CUSTOMER_DECISION: A.WAIT_FOR_CUSTOMER,
    CA.HANDLE_OBJECTION: A.HANDLE_OBJECTION, CA.REVIEW_REVISION: A.REVISE_PROPOSAL,
    CA.CONFIRM_ACCEPTANCE: A.CONFIRM_ACCEPTANCE, CA.COMPLETE_WON: A.MARK_WON, CA.DECIDE_LOSS: A.DECIDE_LOSS,
    CA.CLOSED: A.NO_ACTION, CA.NONE: A.NO_AUTOMATION,
}
# The blocker each operator-gated commercial action itself stands for (Stage 13 lists only
# readiness findings, e.g. an open request blocks a DRAFT but not a presented revision).
COMMERCIAL_ACTION_BLOCKERS: dict[ExecutionAction, ExecutionBlocker] = {
    A.PREPARE_PROPOSAL: B.COMMERCIAL_INPUT_MISSING, A.REVIEW_TERM_REQUEST: B.TERM_REQUEST_OPEN,
    A.REVIEW_PROPOSAL: B.PROPOSAL_REVIEW_REQUIRED, A.PRESENT_PROPOSAL: B.PROPOSAL_NOT_PRESENTED,
    A.HANDLE_OBJECTION: B.OBJECTION_OPEN, A.REVISE_PROPOSAL: B.PROPOSAL_REVISION_REQUIRED,
    A.CONFIRM_ACCEPTANCE: B.ACCEPTANCE_REQUIRES_OPERATOR, A.MARK_WON: B.OPERATOR_REVIEW_REQUIRED,
    A.DECIDE_LOSS: B.OPERATOR_REVIEW_REQUIRED,
}
COMMERCIAL_OWNERS = {NextActionOwner.OPERATOR: O.OPERATOR, NextActionOwner.CUSTOMER: O.CUSTOMER,
                     NextActionOwner.NONE: O.NONE, NextActionOwner.AGENT: O.OPERATOR}
FINGERPRINT_PREFIX = "sxp-"


@dataclass(frozen=True)
class PlannerSettings:
    capabilities: ExecutionCapabilities
    kill_switch: bool


@dataclass(frozen=True)
class Decision:
    owner: ExecutionOwner
    action: ExecutionAction
    subsystem: ExecutionSubsystem
    blockers: tuple[ExecutionBlocker, ...] = ()
    sources: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    refs: EntityRefs = field(default_factory=EntityRefs)
    waiting_until: datetime | None = None


def plan(snapshot: LeadSnapshot, settings: PlannerSettings) -> SalesExecutionPlan:
    decision = decide(snapshot, settings)
    lead = snapshot.pipeline.lead
    kill = (B.KILL_SWITCH_ACTIVE,) if settings.kill_switch and decision.action in OUTBOUND_ACTIONS else ()
    blockers = _ordered((*decision.blockers, *kill))
    executable = decision.action in AUTOMATIC_ACTIONS and not blockers
    body = {
        "lead_id": lead.lead_id, "contact_id": lead.contact_id, "owner": decision.owner, "action": decision.action,
        "subsystem": decision.subsystem, "executable": executable, "requires_operator": decision.owner is O.OPERATOR,
        "requires_customer": decision.owner is O.CUSTOMER, "blockers": blockers,
        "sources": tuple(dict.fromkeys(decision.sources)), "conditions": conditions(snapshot),
        "reasons": tuple(dict.fromkeys(decision.reasons)), "refs": decision.refs,
        "operator_commands": OPERATOR_COMMANDS.get(decision.action, ()) if decision.owner is O.OPERATOR else (),
        "operation": AUTOMATIC_OPERATIONS.get(decision.action), "expected_versions": expected_versions(snapshot),
        "waiting_until": decision.waiting_until, "priority": PRIORITY[decision.action],
        "last_activity_at": last_activity(snapshot),
    }
    material = {"state": state_material(snapshot), "decision": body}
    return SalesExecutionPlan.model_validate(body | {"observed_at": snapshot.now, "fingerprint": fingerprint(material)})


# ---- Ownership decision ------------------------------------------------------------------------


def decide(s: LeadSnapshot, settings: PlannerSettings) -> Decision:
    p = s.pipeline
    lead = p.lead
    if p.suppressed:
        extra = ((B.LEAD_CLOSED,) if lead.stage is LeadStage.CLOSED else ()) + \
            ((B.UNRESOLVED_DISPATCH,) if s.unresolved_outbound_ids else ())
        return Decision(O.NONE, A.NO_AUTOMATION, S.NONE, blockers=(B.DNC, *extra), reasons=("CONTACT_SUPPRESSED",))
    if lead.stage is LeadStage.CLOSED:
        return Decision(O.NONE, A.NO_ACTION, S.NONE, blockers=(B.LEAD_CLOSED,),
                        reasons=(f"LEAD_CLOSED:{lead.close_reason.value if lead.close_reason else 'UNKNOWN'}",))
    if s.unresolved_outbound_ids:
        missing = () if settings.capabilities.reconciliation else (B.PROVIDER_CAPABILITY_MISSING,)
        return Decision(O.DISPATCH_RECOVERY, A.RECONCILE_DISPATCH, S.DISPATCH, blockers=missing,
                        sources=("capability:RECONCILIATION",) if missing else (), reasons=("UNRESOLVED_DISPATCH",),
                        refs=EntityRefs(outbound_ids=s.unresolved_outbound_ids))
    if s.escalations:
        return Decision(O.OPERATOR, A.ESCALATION_REVIEW, S.ESCALATION, blockers=(B.ESCALATION_OPEN,),
                        sources=tuple(f"escalation:{r.value}" for e in s.escalations for r in e.reasons),
                        reasons=("ESCALATION_OPEN",), refs=EntityRefs(escalation_ids=tuple(e.escalation_id for e in s.escalations)))
    if lead.status is LeadStatus.ON_HOLD:
        return Decision(O.OPERATOR, A.ESCALATION_REVIEW, S.ESCALATION, blockers=(B.LEAD_ON_HOLD, B.OPERATOR_REVIEW_REQUIRED),
                        reasons=("LEAD_ON_HOLD",))
    if s.pending_review:
        first = s.pending_review[0]
        campaign = first.kind in CAMPAIGN_KINDS
        return Decision(O.OPERATOR, A.REVIEW_CAMPAIGN_DRAFT if campaign else A.REVIEW_REPLY_DRAFT,
                        S.CAMPAIGN if campaign else S.CONVERSATION, blockers=(B.OPERATOR_REVIEW_REQUIRED,),
                        reasons=(f"DRAFT_PENDING_REVIEW:{first.kind.value}",),
                        refs=EntityRefs(outbound_ids=tuple(m.outbound_id for m in s.pending_review), campaign_id=first.campaign_id,
                                        member_id=p.member.member_id if campaign and p.member else None))
    conversation = p.conversation
    if conversation is not None and conversation.status is ConversationStatus.OPERATOR_REVIEW:
        return Decision(O.OPERATOR, A.ESCALATION_REVIEW, S.CONVERSATION, blockers=(B.OPERATOR_REVIEW_REQUIRED,),
                        reasons=("CONVERSATION_OPERATOR_REVIEW",), refs=EntityRefs(conversation_id=conversation.conversation_id))
    if s.approved:
        return _send(s, settings)
    qualification = p.qualification
    if qualification is not None and qualification.open_conflicts:
        return Decision(O.OPERATOR, A.REVIEW_QUALIFICATION, S.PIPELINE, blockers=(B.QUALIFICATION_CONFLICT,),
                        reasons=("QUALIFICATION_CONFLICT",),
                        refs=EntityRefs(conflict_ids=tuple(c.conflict_id for c in qualification.open_conflicts)))
    if qualification is not None and qualification.status is QualificationStatus.READY_FOR_REVIEW:
        return Decision(O.OPERATOR, A.REVIEW_QUALIFICATION, S.PIPELINE, blockers=(B.OPERATOR_REVIEW_REQUIRED,),
                        reasons=("QUALIFICATION_READY_FOR_REVIEW",))
    if BlockerCode.CUSTOMER_DECLINED in s.pipeline_next.blockers:  # Stage 12's own rule
        return Decision(O.OPERATOR, A.DECIDE_LOSS, S.PIPELINE, blockers=(B.CUSTOMER_DECLINED,),
                        reasons=("CUSTOMER_DECLINED_IN_OPERATOR_STAGE",),
                        refs=EntityRefs(opportunity_id=p.opportunity.opportunity_id if p.opportunity else None))
    if s.commercial is not None and s.commercial_next is not None:
        return _commercial(s, settings)
    if lead.stage is LeadStage.QUALIFIED:
        return Decision(O.OPERATOR, A.CREATE_OPPORTUNITY, S.PIPELINE, blockers=(B.OPPORTUNITY_REQUIRED,),
                        reasons=("QUALIFIED_WITHOUT_OPPORTUNITY",))
    if lead.stage in (LeadStage.OPPORTUNITY, LeadStage.NEGOTIATION):
        return Decision(O.OPERATOR, A.WAIT_FOR_OPERATOR, S.PIPELINE, blockers=(B.OPPORTUNITY_REQUIRED,),
                        reasons=("NO_ACTIVE_OPPORTUNITY",))
    if p.member is not None and p.member.status in IN_SEQUENCE:
        return _campaign(s, settings)
    if s.follow_up is not None:
        return _conversation(s, settings)
    if BlockerCode.NO_ACTIVE_CONVERSATION in s.pipeline_next.blockers:
        return Decision(O.OPERATOR, A.WAIT_FOR_OPERATOR, S.CONVERSATION, blockers=(B.NO_ACTIVE_CONVERSATION,),
                        reasons=("ENGAGED_WITHOUT_ACTIVE_CONVERSATION",))
    return Decision(O.NONE, A.NO_ACTION, S.NONE, reasons=("NOTHING_PENDING",))


def _send(s: LeadSnapshot, settings: PlannerSettings) -> Decision:
    message = s.approved[0]
    campaign = message.kind in CAMPAIGN_KINDS
    blockers: list[ExecutionBlocker] = []
    sources: list[str] = []
    if not settings.capabilities.dispatch:
        blockers.append(B.PROVIDER_CAPABILITY_MISSING)
        sources.append("capability:DISPATCH")
    if s.conflict_outbound_ids:
        blockers.append(B.DISPATCH_ACCEPTANCE_CONFLICT)
    refused = [r for r in s.send_policy_reasons if not (settings.kill_switch and r == "KILL_SWITCH")]
    if refused:
        blockers.append(B.SEND_BLOCKED_BY_POLICY)
        sources += [f"send:{r}" for r in refused]
    member = s.pipeline.member
    return Decision(O.CAMPAIGN if campaign else O.CONVERSATION, A.SEND_APPROVED_MESSAGE, S.DISPATCH,
                    blockers=tuple(blockers), sources=tuple(sources), reasons=(f"OPERATOR_APPROVED:{message.kind.value}",),
                    refs=EntityRefs(outbound_ids=(message.outbound_id,), campaign_id=message.campaign_id,
                                    member_id=member.member_id if campaign and member else None,
                                    conversation_id=s.pipeline.conversation.conversation_id
                                    if not campaign and s.pipeline.conversation else None))


def _commercial(s: LeadSnapshot, settings: PlannerSettings) -> Decision:
    facts, next_ = s.commercial, s.commercial_next
    assert facts is not None and next_ is not None
    action = COMMERCIAL_ACTIONS[next_.action]
    owner = COMMERCIAL_OWNERS[next_.owner]
    current = facts.current
    refs = EntityRefs(
        opportunity_id=facts.opportunity.opportunity_id, proposal_id=current.proposal_id if current else None,
        revision_id=current.revision_id if current else None,
        request_ids=tuple(r.request_id for r in facts.open_requests),
        objection_ids=tuple(o.objection_id for o in facts.open_objections),
        signal_ids=tuple(x.signal_id for k in SignalKind for x in facts.open_signals(k)),
    )
    blockers = tuple(COMMERCIAL_BLOCKERS[b] for b in next_.blockers)
    sources = tuple(f"commercial:{b.value}" for b in next_.blockers)
    if owner is O.CUSTOMER:
        # Waiting for the customer's decision: a follow-up the Stage 9 policy permits still runs.
        follow_up = _conversation(s, settings) if s.follow_up is not None else None
        if follow_up is not None and follow_up.action is A.PROCESS_FOLLOW_UP:
            return follow_up
        return Decision(O.CUSTOMER, A.WAIT_FOR_CUSTOMER, S.COMMERCIAL, blockers=(B.WAITING_FOR_CUSTOMER,),
                        sources=sources, reasons=("WAITING_FOR_PROPOSAL_DECISION",), refs=refs,
                        waiting_until=follow_up.waiting_until if follow_up is not None else None)
    own = COMMERCIAL_ACTION_BLOCKERS.get(action, B.OPERATOR_REVIEW_REQUIRED)
    return Decision(owner, action, S.COMMERCIAL, blockers=(own, *blockers), sources=sources,
                    reasons=(f"COMMERCIAL:{next_.action.value}",), refs=refs)


def _campaign(s: LeadSnapshot, settings: PlannerSettings) -> Decision:
    member, campaign, pending, now = s.pipeline.member, s.campaign, s.pending_touch, s.now
    assert member is not None
    job = pending.open_job if pending else None
    refs = EntityRefs(campaign_id=member.campaign_id, member_id=member.member_id, job_id=job.job_id if job else None)
    waiting = Decision(O.CUSTOMER, A.WAIT_FOR_CUSTOMER, S.CAMPAIGN, blockers=(B.WAITING_FOR_CUSTOMER,),
                       reasons=("CAMPAIGN_AWAITING_REPLY",), refs=refs,
                       waiting_until=pending.due_at if pending is not None else None)
    if pending is None:
        if member.status is CampaignMemberStatus.WAITING:
            return waiting
        return Decision(O.OPERATOR, A.WAIT_FOR_OPERATOR, S.CAMPAIGN, blockers=(B.OPERATOR_REVIEW_REQUIRED,),
                        reasons=(f"CAMPAIGN_MEMBER:{member.status.value}",), refs=refs)
    inactive = campaign is None or campaign.status is not CampaignStatus.ACTIVE
    inactive_source = (f"campaign:{campaign.status.value if campaign else 'MISSING'}",) if inactive else ()
    if pending.due_at > now:
        if member.status is CampaignMemberStatus.WAITING:
            return waiting
        return Decision(O.CAMPAIGN, A.PREPARE_CAMPAIGN_TOUCH, S.CAMPAIGN, blockers=(B.NOT_DUE,),
                        reasons=("FIRST_TOUCH_NOT_DUE",), refs=refs, waiting_until=pending.due_at)
    if pending.exhaust:
        return Decision(O.CAMPAIGN, A.COMPLETE_CAMPAIGN_SEQUENCE, S.CAMPAIGN,
                        blockers=(B.CAMPAIGN_NOT_ACTIVE,) if inactive else (), sources=inactive_source,
                        reasons=("CAMPAIGN_SEQUENCE_COMPLETE",), refs=refs)
    blockers: list[ExecutionBlocker] = []
    if job is not None and job.status is CampaignJobStatus.CLAIMED and job.lease_expires_at and job.lease_expires_at > now:
        blockers.append(B.WORK_IN_PROGRESS)
    if inactive:
        blockers.append(B.CAMPAIGN_NOT_ACTIVE)
    if s.conflict_outbound_ids:
        blockers.append(B.DISPATCH_ACCEPTANCE_CONFLICT)
    return Decision(O.CAMPAIGN, A.PREPARE_CAMPAIGN_TOUCH, S.CAMPAIGN, blockers=tuple(blockers), sources=inactive_source,
                    reasons=(f"CAMPAIGN_TOUCH_DUE:{pending.touch_no}",), refs=refs)


def _conversation(s: LeadSnapshot, settings: PlannerSettings) -> Decision:
    facts, now = s.follow_up, s.now
    assert facts is not None
    conversation = facts.conversation
    job = facts.open_job
    refs = EntityRefs(conversation_id=conversation.conversation_id, follow_up_id=job.follow_up_id if job else None)
    waiting = Decision(O.CUSTOMER, A.WAIT_FOR_CUSTOMER, S.CONVERSATION, blockers=(B.WAITING_FOR_CUSTOMER,),
                       sources=tuple(f"follow_up:{c}" for c in s.follow_up_schedule_blockers
                                     if c != "FOLLOW_UP_ALREADY_SCHEDULED"),
                       reasons=("AWAITING_CUSTOMER_REPLY",), refs=refs)
    status = conversation.status
    if status is ConversationStatus.ACTIVE:
        qualify = any(g.reason == "MISSING_REQUIRED" and g.safe_to_ask for g in s.pipeline.gaps)
        # The customer wrote last and nothing is drafted: Stage 6 already ran for that message
        # (replays never draft twice) and no standalone composer exists, so a human responds.
        blockers = [B.OPERATOR_REVIEW_REQUIRED, *([B.QUALIFICATION_INCOMPLETE] if qualify else [])]
        if not settings.capabilities.llm:
            blockers.append(B.LLM_CAPABILITY_MISSING)
        return Decision(O.OPERATOR, A.QUALIFY_LEAD if qualify else A.RESPOND_TO_CUSTOMER,
                        S.PIPELINE if qualify else S.CONVERSATION, blockers=tuple(blockers),
                        sources=("capability:LLM",) if not settings.capabilities.llm else (),
                        reasons=("CUSTOMER_WROTE_LAST",), refs=refs)
    if status is ConversationStatus.PAUSED:
        return Decision(O.OPERATOR, A.WAIT_FOR_OPERATOR, S.CONVERSATION, blockers=(B.CONVERSATION_PAUSED,),
                        reasons=("CONVERSATION_PAUSED",), refs=refs)
    if status is ConversationStatus.WAITING_FOR_REPLY:
        if s.follow_up_schedule_blockers:
            return waiting
        return Decision(O.CONVERSATION, A.PROCESS_FOLLOW_UP, S.CONVERSATION,
                        reasons=("FOLLOW_UP_SCHEDULABLE",), refs=refs)
    if status is ConversationStatus.FOLLOW_UP_DUE and job is not None:
        if job.status is FollowUpJobStatus.SCHEDULED and job.due_at > now:
            return replace(waiting, waiting_until=job.due_at, reasons=("FOLLOW_UP_NOT_DUE",))
        blockers = []
        if job.status is FollowUpJobStatus.CLAIMED and job.lease_expires_at and job.lease_expires_at > now:
            blockers.append(B.WORK_IN_PROGRESS)
        return Decision(O.CONVERSATION, A.PROCESS_FOLLOW_UP, S.CONVERSATION, blockers=tuple(blockers),
                        reasons=(f"FOLLOW_UP_DUE:{job.sequence_no}",), refs=refs)
    return waiting


# ---- Context, versions, activity -----------------------------------------------------------------


def conditions(s: LeadSnapshot) -> tuple[ExecutionBlocker, ...]:
    """Every normalized cross-subsystem condition (context; not all of them block the action)."""
    found = [PIPELINE_BLOCKERS[b] for b in s.pipeline_next.blockers]
    if s.commercial_next is not None:
        found += [COMMERCIAL_BLOCKERS[b] for b in s.commercial_next.blockers]
    if s.unresolved_outbound_ids:
        found.append(B.UNRESOLVED_DISPATCH)
    if s.conflict_outbound_ids:
        found.append(B.DISPATCH_ACCEPTANCE_CONFLICT)
    if s.escalations:
        found.append(B.ESCALATION_OPEN)
    return _ordered(found)


def expected_versions(s: LeadSnapshot) -> dict[str, int]:
    p = s.pipeline
    versions = {"lead": p.lead.version}
    if p.qualification is not None:
        versions["qualification"] = p.qualification.version
    if p.opportunity is not None:
        versions["opportunity"] = p.opportunity.version
    if p.conversation is not None:
        versions["conversation"] = p.conversation.version
    if p.member is not None:
        versions["campaign_member"] = p.member.version
    if s.commercial is not None and s.commercial.current is not None:
        versions["proposal_revision"] = s.commercial.current.version
    return versions


def last_activity(s: LeadSnapshot) -> datetime:
    p = s.pipeline
    moments = [p.lead.updated_at]
    if p.conversation is not None:
        moments.append(p.conversation.last_activity_at)
    if p.member is not None:
        moments.append(p.member.last_activity_at)
    if p.opportunity is not None:
        moments.append(p.opportunity.updated_at)
    return max(moments)


def _ordered(blockers: object) -> tuple[ExecutionBlocker, ...]:
    present = set(blockers)  # type: ignore[call-overload]
    return tuple(b for b in ExecutionBlocker if b in present)


# ---- Fingerprint ---------------------------------------------------------------------------------


def state_material(s: LeadSnapshot) -> dict[str, object]:
    """The normalized durable state the decision rests on: IDs, statuses, versions, due
    times and codes. Never a subject, body, summary or any other free text."""
    p = s.pipeline
    lead, q, opp, conv, member = p.lead, p.qualification, p.opportunity, p.conversation, p.member
    job = s.follow_up.open_job if s.follow_up else None
    touch = s.pending_touch
    commercial = s.commercial
    return {
        "lead": [lead.lead_id, lead.version, lead.stage, lead.status, lead.close_reason, lead.last_intent],
        "suppressed": p.suppressed,
        "qualification": [q.status, q.version, len(q.open_conflicts)] if q else None,
        "opportunity": [opp.opportunity_id, opp.status, opp.version] if opp else None,
        "conversation": [conv.conversation_id, conv.status, conv.version, conv.association_certain] if conv else None,
        "follow_up_job": [job.follow_up_id, job.status, job.version, job.due_at, job.lease_expires_at] if job else None,
        "follow_up_blockers": list(s.follow_up_schedule_blockers),
        "member": [member.member_id, member.status, member.version, member.touch_count] if member else None,
        "campaign": [s.campaign.campaign_id, s.campaign.status, s.campaign.version] if s.campaign else None,
        "pending_touch": [touch.touch_no, touch.due_at, touch.exhaust,
                          [touch.open_job.job_id, touch.open_job.status, touch.open_job.version] if touch.open_job else None]
        if touch else None,
        "messages": sorted([m.outbound_id, m.lead_id, m.kind, m.status, m.version] for m in s.contact_messages),
        "attempts": sorted([a.attempt_id, a.outbound_id, a.state, a.late_acceptance_provider_message_id is not None]
                           for a in s.attempts),
        "escalations": [[e.escalation_id, e.status, e.version] for e in s.escalations],
        "send_policy": list(s.send_policy_reasons),
        "commercial": None if commercial is None else {
            "revisions": [[r.revision_id, r.status, r.version] for r in commercial.revisions],
            "terms": sorted([t.term_row_id, t.version] for t in commercial.terms),
            "requests": sorted([r.request_id, r.status, r.version] for r in commercial.requests),
            "objections": sorted([o.objection_id, o.status, o.version] for o in commercial.objections),
            "signals": sorted([x.signal_id, x.kind, x.status, x.version] for x in commercial.signals),
        },
    }


def fingerprint(material: dict[str, object]) -> str:
    text = json.dumps(_jsonable(material), sort_keys=True, separators=(",", ":"))
    return FINGERPRINT_PREFIX + hashlib.sha256(text.encode("utf-8")).hexdigest()[:40]


def _jsonable(value: object) -> object:
    if isinstance(value, str | int | float | bool) or value is None:  # StrEnum is a str
        return value
    if isinstance(value, CoreModel):
        return _jsonable(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"not part of a plan fingerprint: {type(value).__name__}")
