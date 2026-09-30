"""Operator-driven commercial lifecycle: opportunity, negotiation, WON, LOST, disqualify,
reopen, and the automation stop that every terminal decision triggers.

Only operator commands call these (an ``operator_id`` is required); no AI output does.
Terminal effects reuse the existing services, in the same transaction:
- campaign (Stage 10): WON converts the membership, any other close ends one that is
  still in sequence (its job, undispatched campaign drafts and plan stop);
- conversation (Stage 9): every conversation of the lead ends (CONVERTED when won,
  else CLOSED), its follow-up job is cancelled, undispatched follow-up drafts too;
- every other not-yet-dispatched message of the lead is cancelled and its ACTIVE quota
  reservation released (``cancel_undispatched``: once, and never a CONSUMED one);
- dispatched history (SENDING and later) and accepted sends are never touched;
- DNC is independent: WON or LOST neither adds nor removes suppression.
Reopen changes only the commercial position: the lead becomes OPERATOR_OWNED at an early
stage; no conversation, campaign membership, job, draft or opportunity is resurrected and
no automation restarts by itself.
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from app.campaign.state import record_lead_closed as close_campaign_membership
from app.conversation.state import record_lead_closed as close_conversations
from app.conversation.cancellation import cancel_undispatched
from app.core.enums import (
    CloseReason,
    DisqualificationReason,
    FollowUpCancelReason,
    FollowUpStatus,
    LeadStage,
    LeadStatus,
    LostReason,
    OpportunityStatus,
    PipelineTrigger,
    QualificationStatus,
    RefKind,
)
from app.core.models import FollowUpPlan, Lead, LeadQualification, Opportunity
from app.inbound.models import stable_id
from app.persistence import AlreadyExistsError, UnitOfWork
from app.pipeline.audit import PIPELINE_ACTOR, operator_actor, record_event, ref
from app.pipeline.config import PipelineConfig, QualificationProfile
from app.pipeline.errors import PipelineCode, PipelineError, PipelineNotFoundError
from app.pipeline.guards import require_not_suppressed, require_open
from app.pipeline.policy import apply_transition
from app.pipeline.qualification import DECIDED, readiness

DISQUALIFY_CLOSE_REASON = {
    DisqualificationReason.CUSTOMER_DECLINED: CloseReason.NOT_INTERESTED,
    DisqualificationReason.DUPLICATE_OR_INVALID_LEAD: CloseReason.DUPLICATE,
}


@dataclass(frozen=True)
class AutomationStop:
    cancelled_outbound_ids: tuple[str, ...]
    released_reservation_ids: tuple[str, ...]


def stop_automation(uow: UnitOfWork, lead: Lead, *, correlation_id: str, now: datetime) -> AutomationStop:
    """Terminal lead effects through the existing Stage 9/10 services (see module doc)."""
    close_campaign_membership(uow, lead, correlation_id=correlation_id, now=now)
    close_conversations(uow, lead, correlation_id=correlation_id, now=now)
    cancelled, released = cancel_undispatched(uow, uow.outbound.list_by_lead(lead.lead_id), now)
    plan = uow.follow_ups.get_open_for_lead(lead.lead_id)
    if plan is not None:
        uow.follow_ups.update(FollowUpPlan.model_validate(plan.model_dump() | {
            "status": FollowUpStatus.CANCELLED, "cancel_reason": FollowUpCancelReason.LEAD_CLOSED, "next_due_at": None,
            "updated_at": max(now, plan.updated_at), "version": plan.version + 1,
        }), plan.version)
    stop = AutomationStop(tuple(m.outbound_id for m in cancelled), tuple(released))
    record_event(uow, key=(lead.lead_id, str(lead.version)), event_type="LEAD_AUTOMATION_STOPPED",
                 subjects=(ref(RefKind.LEAD, lead.lead_id), *(ref(RefKind.OUTBOUND_MESSAGE, i) for i in stop.cancelled_outbound_ids)),
                 after={"close_reason": lead.close_reason.value if lead.close_reason else None,
                        "cancelled_outbound_ids": list(stop.cancelled_outbound_ids),
                        "released_reservation_ids": list(stop.released_reservation_ids)},
                 correlation_id=correlation_id, now=now)
    return stop


def create_opportunity(
    uow: UnitOfWork, lead: Lead, *, amount: Decimal | None, currency: str | None, scope: str | None,
    expected_decision_date: date | None, next_step: str | None, operator_id: str, command_id: str,
    correlation_id: str, now: datetime,
) -> tuple[Lead, Opportunity]:
    require_open(lead)
    require_not_suppressed(uow, lead, now)
    qualification = uow.qualifications.get(lead.lead_id)
    if qualification is None or qualification.status is not QualificationStatus.QUALIFIED:
        raise PipelineError(PipelineCode.QUALIFICATION_NOT_READY)
    if qualification.open_conflicts:  # e.g. a disagreeing fact arrived after the approval
        raise PipelineError(PipelineCode.QUALIFICATION_CONFLICT_OPEN)
    if uow.opportunities.get_active_for_lead(lead.lead_id) is not None:
        raise PipelineError(PipelineCode.OPPORTUNITY_EXISTS)
    lead = apply_transition(uow, lead, PipelineTrigger.OPPORTUNITY_CREATED, LeadStage.OPPORTUNITY,
                            actor=operator_actor(operator_id), correlation_id=correlation_id, now=now,
                            reason="OPPORTUNITY_CREATED", command_id=command_id)
    opportunity = Opportunity(
        opportunity_id=stable_id("op", lead.lead_id, command_id), lead_id=lead.lead_id, amount=amount,
        currency=currency, scope=scope, expected_decision_date=expected_decision_date, next_step=next_step,
        owner_operator_id=operator_id, created_at=now, updated_at=now,
    )
    try:
        uow.opportunities.add(opportunity)
    except AlreadyExistsError:  # SQL: at most one active opportunity per lead
        raise PipelineError(PipelineCode.OPPORTUNITY_EXISTS) from None
    record_event(uow, key=(opportunity.opportunity_id, "1"), event_type="OPPORTUNITY_CREATED",
                 subjects=(ref(RefKind.OPPORTUNITY, opportunity.opportunity_id), ref(RefKind.LEAD, lead.lead_id)),
                 after={"status": opportunity.status.value, "amount": str(amount) if amount is not None else None,
                        "currency": currency, "expected_decision_date": expected_decision_date.isoformat() if expected_decision_date else None},
                 actor=operator_actor(operator_id), correlation_id=correlation_id, now=now)
    return lead, opportunity


def active_opportunity(uow: UnitOfWork, lead: Lead, opportunity_id: str, expected_version: int) -> Opportunity:
    opportunity = uow.opportunities.get(opportunity_id)
    if opportunity is None or opportunity.lead_id != lead.lead_id:
        raise PipelineNotFoundError(f"opportunity {opportunity_id} not found for lead {lead.lead_id}")
    if opportunity.version != expected_version:
        raise PipelineError(PipelineCode.OPPORTUNITY_VERSION_CHANGED)
    if opportunity.status not in (OpportunityStatus.OPEN, OpportunityStatus.NEGOTIATING):
        raise PipelineError(PipelineCode.OPPORTUNITY_NOT_ACTIVE)
    return opportunity


def start_negotiation(
    uow: UnitOfWork, lead: Lead, *, opportunity_id: str, expected_opportunity_version: int, operator_id: str,
    command_id: str, correlation_id: str, now: datetime,
) -> tuple[Lead, Opportunity]:
    require_open(lead)
    opportunity = active_opportunity(uow, lead, opportunity_id, expected_opportunity_version)
    require_not_suppressed(uow, lead, now)
    if opportunity.status is not OpportunityStatus.OPEN:
        raise PipelineError(PipelineCode.OPPORTUNITY_NOT_ACTIVE)
    lead = apply_transition(uow, lead, PipelineTrigger.NEGOTIATION_STARTED, LeadStage.NEGOTIATION,
                            actor=operator_actor(operator_id), correlation_id=correlation_id, now=now,
                            reason="NEGOTIATION_STARTED", command_id=command_id)
    return lead, _save_opportunity(uow, opportunity, OpportunityStatus.NEGOTIATING, operator_id=operator_id,
                                   correlation_id=correlation_id, now=now)


def mark_won(
    uow: UnitOfWork, lead: Lead, *, opportunity_id: str, expected_opportunity_version: int, operator_id: str,
    command_id: str, correlation_id: str, now: datetime,
) -> tuple[Lead, AutomationStop]:
    """Commercially terminal. Needs an active opportunity; DNC blocks it (suppression wins)."""
    require_open(lead)
    opportunity = active_opportunity(uow, lead, opportunity_id, expected_opportunity_version)
    require_not_suppressed(uow, lead, now)
    lead = apply_transition(uow, lead, PipelineTrigger.OPERATOR_MARKED_WON, LeadStage.CLOSED,
                            close_reason=CloseReason.WON, actor=operator_actor(operator_id),
                            correlation_id=correlation_id, now=now, reason="WON", command_id=command_id)
    _save_opportunity(uow, opportunity, OpportunityStatus.WON, operator_id=operator_id, correlation_id=correlation_id, now=now)
    return lead, stop_automation(uow, lead, correlation_id=correlation_id, now=now)


def mark_lost(
    uow: UnitOfWork, lead: Lead, *, reason: LostReason, operator_id: str, command_id: str, correlation_id: str,
    now: datetime,
) -> tuple[Lead, AutomationStop]:
    """Commercially terminal from any open stage. Not DNC: the contact stays contactable
    by a future explicit action unless it is (separately) suppressed."""
    require_open(lead)
    active = uow.opportunities.get_active_for_lead(lead.lead_id)
    lead = apply_transition(uow, lead, PipelineTrigger.OPERATOR_MARKED_LOST, LeadStage.CLOSED,
                            close_reason=CloseReason.LOST, actor=operator_actor(operator_id),
                            correlation_id=correlation_id, now=now, reason=reason.value, command_id=command_id)
    if active is not None:
        _save_opportunity(uow, active, OpportunityStatus.LOST, operator_id=operator_id, correlation_id=correlation_id,
                          now=now, lost_reason=reason)
    return lead, stop_automation(uow, lead, correlation_id=correlation_id, now=now)


def disqualify(
    uow: UnitOfWork, profile: QualificationProfile, lead: Lead, *, reason: DisqualificationReason, operator_id: str,
    command_id: str, correlation_id: str, now: datetime,
) -> tuple[Lead, AutomationStop]:
    """Not "not qualified yet": a reasoned operator decision that closes the lead. With an
    active opportunity the lead must be marked LOST instead."""
    require_open(lead)
    if uow.opportunities.get_active_for_lead(lead.lead_id) is not None:
        raise PipelineError(PipelineCode.OPPORTUNITY_ACTIVE)
    lead = apply_transition(uow, lead, PipelineTrigger.DISQUALIFIED, LeadStage.CLOSED,
                            close_reason=DISQUALIFY_CLOSE_REASON.get(reason, CloseReason.DISQUALIFIED),
                            actor=operator_actor(operator_id), correlation_id=correlation_id, now=now,
                            reason=reason.value, command_id=command_id)
    current = uow.qualifications.get(lead.lead_id)
    decided = {"status": QualificationStatus.DISQUALIFIED, "disqualification_reason": reason, "decided_by": operator_id,
               "decided_at": now}
    if current is None:
        uow.qualifications.add(LeadQualification.model_validate(
            {"lead_id": lead.lead_id, "profile_id": profile.profile_id, "created_at": now, "updated_at": now} | decided))
    else:
        uow.qualifications.update(LeadQualification.model_validate(current.model_dump() | decided | {
            "updated_at": max(now, current.updated_at), "version": current.version + 1}), current.version)
    record_event(uow, key=(lead.lead_id, "disqualified", command_id), event_type="QUALIFICATION_DISQUALIFIED",
                 subjects=(ref(RefKind.LEAD_QUALIFICATION, lead.lead_id), ref(RefKind.LEAD, lead.lead_id)),
                 before={"status": current.status.value if current else QualificationStatus.NOT_STARTED.value},
                 after={"status": QualificationStatus.DISQUALIFIED.value, "reason": reason.value},
                 actor=operator_actor(operator_id), correlation_id=correlation_id, now=now)
    return lead, stop_automation(uow, lead, correlation_id=correlation_id, now=now)


def reopen(
    uow: UnitOfWork, config: PipelineConfig, lead: Lead, *, target: LeadStage, operator_id: str, command_id: str,
    correlation_id: str, now: datetime,
) -> Lead:
    if target not in config.reopen_targets:
        raise PipelineError(PipelineCode.REOPEN_TARGET_NOT_ALLOWED)
    require_not_suppressed(uow, lead, now)
    closed_reason = lead.close_reason
    lead = apply_transition(uow, lead, PipelineTrigger.OPERATOR_REOPENED, target, status=LeadStatus.OPERATOR_OWNED,
                            actor=operator_actor(operator_id), correlation_id=correlation_id, now=now,
                            reason=f"REOPENED_FROM_{closed_reason.value if closed_reason else 'UNKNOWN'}",
                            command_id=command_id)
    current = uow.qualifications.get(lead.lead_id)
    if current is not None and current.status in DECIDED:
        draft = current.model_copy(update={"status": QualificationStatus.IN_PROGRESS, "disqualification_reason": None,
                                           "decided_by": None, "decided_at": None,
                                           "updated_at": max(now, current.updated_at), "version": current.version + 1})
        reopened = LeadQualification.model_validate(draft.model_dump() | {"status": readiness(config.profile, draft)})
        uow.qualifications.update(reopened, current.version)
    return lead


def cancel_opportunity_of_closed_lead(uow: UnitOfWork, lead: Lead, *, correlation_id: str, now: datetime) -> Opportunity | None:
    """A lead closed outside the operator pipeline (e.g. Stage 6 closing it on an unsubscribe:
    suppression wins over any stage) must not keep an active opportunity. It is CANCELLED,
    not LOST: no commercial judgement was made."""
    if lead.stage is not LeadStage.CLOSED:
        return None
    active = uow.opportunities.get_active_for_lead(lead.lead_id)
    if active is None:
        return None
    return _save_opportunity(uow, active, OpportunityStatus.CANCELLED, operator_id=None, correlation_id=correlation_id, now=now)


def _save_opportunity(
    uow: UnitOfWork, opportunity: Opportunity, status: OpportunityStatus, *, operator_id: str | None, correlation_id: str,
    now: datetime, lost_reason: LostReason | None = None,
) -> Opportunity:
    closed = status not in (OpportunityStatus.OPEN, OpportunityStatus.NEGOTIATING)
    updated = Opportunity.model_validate(opportunity.model_dump() | {
        "status": status, "lost_reason": lost_reason, "closed_at": now if closed else None,
        "updated_at": max(now, opportunity.updated_at), "version": opportunity.version + 1,
    })
    uow.opportunities.update(updated, opportunity.version)
    record_event(uow, key=(opportunity.opportunity_id, str(updated.version)), event_type="OPPORTUNITY_STATUS_CHANGED",
                 subjects=(ref(RefKind.OPPORTUNITY, opportunity.opportunity_id), ref(RefKind.LEAD, opportunity.lead_id)),
                 before={"status": opportunity.status.value},
                 after={"status": status.value, "lost_reason": lost_reason.value if lost_reason else None},
                 actor=operator_actor(operator_id) if operator_id else PIPELINE_ACTOR, correlation_id=correlation_id, now=now)
    return updated
