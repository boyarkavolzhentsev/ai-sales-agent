"""Deterministic commercial state: facts, proposal readiness, revision-required, blockers
and the next commercial action. ``gather`` reads; everything else is pure."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from app.commercial.config import CommercialProfile
from app.commercial.money import proposal_totals
from app.commercial.terms import effective_terms, resolve
from app.core.enums import (
    CommercialAction,
    CommercialBlocker,
    CommercialStage,
    LeadStage,
    NextActionOwner,
    ObjectionStatus,
    QualificationStatus,
    RevisionStatus,
    SignalKind,
    SignalStatus,
    TermSource,
    TermType,
)
from app.core.models import (
    ACTIVE_OPPORTUNITY_STATUSES,
    OPEN_OBJECTION_STATUSES,
    OPEN_REQUEST_STATUSES,
    OPEN_REVISION_STATUSES,
    CommercialSignal,
    CommercialTerm,
    Lead,
    LeadQualification,
    Objection,
    Opportunity,
    ProposalRevision,
    ProposalTotals,
    TermRequest,
)
from app.core.models.base import CoreModel
from app.core.models.commercial import MAIN_KEY
from app.persistence import UnitOfWork
from app.pipeline.guards import is_suppressed


class ReadinessLevel(StrEnum):
    NOT_READY = "NOT_READY"
    READY_FOR_DRAFT = "READY_FOR_DRAFT"  # a proposal may be created or edited
    READY_FOR_REVIEW = "READY_FOR_REVIEW"  # the current draft may be approved


class ProposalReadiness(CoreModel):
    level: ReadinessLevel
    missing: tuple[str, ...] = ()  # e.g. "PRICE:l1", "TERM:PAYMENT_TERM"
    blockers: tuple[CommercialBlocker, ...] = ()
    warnings: tuple[CommercialBlocker, ...] = ()  # open objections: visible, not blocking


class CommercialNextAction(CoreModel):
    owner: NextActionOwner
    action: CommercialAction
    blockers: tuple[CommercialBlocker, ...] = ()


@dataclass(frozen=True)
class CommercialFacts:
    lead: Lead
    opportunity: Opportunity
    suppressed: bool
    qualification: LeadQualification | None
    revisions: tuple[ProposalRevision, ...]
    terms: tuple[CommercialTerm, ...]
    requests: tuple[TermRequest, ...]
    objections: tuple[Objection, ...]
    signals: tuple[CommercialSignal, ...]
    now: datetime

    @property
    def current(self) -> ProposalRevision | None:
        """The open revision if any, else the latest one."""
        open_ = [r for r in self.revisions if r.status in OPEN_REVISION_STATUSES]
        return open_[0] if open_ else (self.revisions[-1] if self.revisions else None)

    @property
    def open_requests(self) -> tuple[TermRequest, ...]:
        return tuple(r for r in self.requests if r.status in OPEN_REQUEST_STATUSES)

    @property
    def open_objections(self) -> tuple[Objection, ...]:
        return tuple(o for o in self.objections if o.status in OPEN_OBJECTION_STATUSES)

    def open_signals(self, kind: SignalKind) -> tuple[CommercialSignal, ...]:
        return tuple(s for s in self.signals if s.status is SignalStatus.OPEN and s.kind is kind)


def gather(uow: UnitOfWork, opportunity: Opportunity, now: datetime) -> CommercialFacts:
    lead = uow.leads.get(opportunity.lead_id)
    assert lead is not None  # a foreign key guarantees it
    return CommercialFacts(
        lead=lead, opportunity=opportunity, suppressed=is_suppressed(uow, lead, now),
        qualification=uow.qualifications.get(lead.lead_id),
        revisions=tuple(uow.proposal_revisions.list_for_opportunity(opportunity.opportunity_id)),
        terms=tuple(uow.commercial_terms.list_for_opportunity(opportunity.opportunity_id)),
        requests=tuple(uow.term_requests.list_for_opportunity(opportunity.opportunity_id)),
        objections=tuple(uow.objections.list_for_opportunity(opportunity.opportunity_id)),
        signals=tuple(uow.commercial_signals.list_for_opportunity(opportunity.opportunity_id)),
        now=now,
    )


def base_blockers(f: CommercialFacts) -> list[CommercialBlocker]:
    found: list[CommercialBlocker] = []
    if f.suppressed:
        found.append(CommercialBlocker.DNC)
    if f.lead.stage is LeadStage.CLOSED:
        found.append(CommercialBlocker.LEAD_CLOSED)
    if f.opportunity.status not in ACTIVE_OPPORTUNITY_STATUSES:
        found.append(CommercialBlocker.OPPORTUNITY_NOT_OPEN)
    if f.qualification is None or f.qualification.status is not QualificationStatus.QUALIFIED:
        found.append(CommercialBlocker.QUALIFICATION_NOT_APPROVED)
    elif f.qualification.open_conflicts:
        found.append(CommercialBlocker.QUALIFICATION_CONFLICT)
    return found


def draft_findings(profile: CommercialProfile, f: CommercialFacts, draft: ProposalRevision) -> tuple[list[str], list[CommercialBlocker]]:
    """What the draft still lacks (missing inputs) and why it cannot be approved."""
    missing: list[str] = []
    blockers: list[CommercialBlocker] = []
    if draft.currency not in profile.currencies:
        blockers.append(CommercialBlocker.CURRENCY_NOT_ALLOWED)
    if not draft.lines:
        blockers.append(CommercialBlocker.NO_PROPOSAL_LINES)
    unpriced = [line.line_id for line in draft.lines if line.unit_price is None]
    if unpriced:
        blockers.append(CommercialBlocker.MISSING_PRICE)
        missing += [f"PRICE:{line_id}" for line_id in unpriced]
    terms = list(f.terms)
    absent = [t for t in profile.required_terms if resolve(profile, draft, terms, t, MAIN_KEY, f.now) is None]
    if absent:
        blockers.append(CommercialBlocker.MISSING_REQUIRED_TERM)
        missing += [f"TERM:{t.value}" for t in absent]
    if not _discounts_allowed(profile, f, draft):
        blockers.append(CommercialBlocker.DISCOUNT_NOT_ALLOWED)
    if f.open_requests:
        blockers.append(CommercialBlocker.UNAPPROVED_TERM_REQUEST)
    return missing, blockers


def _discounts_allowed(profile: CommercialProfile, f: CommercialFacts, draft: ProposalRevision) -> bool:
    policy = profile.discount_policy
    if policy is None:
        return True
    discount = resolve(profile, draft, list(f.terms), TermType.DISCOUNT, MAIN_KEY, f.now)
    if discount is not None and discount.value.percent is not None and discount.value.percent > policy.max_percent:
        return False
    return all(line.discount_percent is None or (line.discount_percent <= policy.max_percent
                                                 and line.item_ref not in policy.forbidden_item_refs)
               for line in draft.lines)


def readiness(profile: CommercialProfile, f: CommercialFacts) -> ProposalReadiness:
    warnings = (CommercialBlocker.OPEN_OBJECTION,) if f.open_objections else ()
    base = base_blockers(f)
    if base:
        return ProposalReadiness(level=ReadinessLevel.NOT_READY, blockers=tuple(base), warnings=warnings)
    current = f.current
    if current is None:
        return ProposalReadiness(level=ReadinessLevel.READY_FOR_DRAFT, missing=("PROPOSAL",),
                                 blockers=(CommercialBlocker.NO_PROPOSAL,), warnings=warnings)
    if current.status is not RevisionStatus.DRAFT:
        return ProposalReadiness(level=ReadinessLevel.READY_FOR_DRAFT, warnings=warnings)
    missing, blockers = draft_findings(profile, f, current)
    level = ReadinessLevel.READY_FOR_REVIEW if not blockers else ReadinessLevel.READY_FOR_DRAFT
    return ProposalReadiness(level=level, missing=tuple(missing), blockers=tuple(blockers), warnings=warnings)


def draft_totals(profile: CommercialProfile, f: CommercialFacts, draft: ProposalRevision) -> ProposalTotals | None:
    if draft.currency not in profile.currencies:
        return None
    discount = resolve(profile, draft, list(f.terms), TermType.DISCOUNT, MAIN_KEY, f.now)
    return proposal_totals(draft.lines, draft.currency, profile.decimals(draft.currency),
                           discount.value.percent if discount else None)


def revision_required(profile: CommercialProfile, f: CommercialFacts) -> bool:
    """An approved or presented revision no longer matches what is approved now: a term
    changed (or appeared) at the opportunity/profile level, or a request is open."""
    current = f.current
    if current is None or current.status not in (RevisionStatus.APPROVED, RevisionStatus.PRESENTED):
        return False
    if f.open_requests:
        return True
    frozen = {(t.term_type, t.term_key): t for t in current.frozen_terms if t.provenance.source is not TermSource.REVISION_OVERRIDE}
    overridden = {(t.term_type, t.term_key) for t in current.frozen_terms if t.provenance.source is TermSource.REVISION_OVERRIDE}
    for term in effective_terms(profile, None, list(f.terms), f.now):
        key = (term.term_type, term.term_key)
        if key in overridden:
            continue
        if key not in frozen or not frozen[key].value.same_as(term.value):
            return True
    return False


def stage(profile: CommercialProfile, f: CommercialFacts) -> CommercialStage:
    if f.lead.stage is LeadStage.CLOSED or f.opportunity.status not in ACTIVE_OPPORTUNITY_STATUSES:
        return CommercialStage.CLOSED
    current = f.current
    if current is None:
        return CommercialStage.NOT_STARTED
    if current.status is RevisionStatus.DRAFT:
        ready = readiness(profile, f).level is ReadinessLevel.READY_FOR_REVIEW
        return CommercialStage.READY_FOR_REVIEW if ready else (
            CommercialStage.NEGOTIATING if current.revision > 1 else CommercialStage.PREPARING)
    if current.status is RevisionStatus.PRESENTED and (f.open_requests or f.open_objections):
        return CommercialStage.NEGOTIATING
    return {RevisionStatus.APPROVED: CommercialStage.APPROVED, RevisionStatus.PRESENTED: CommercialStage.PRESENTED,
            RevisionStatus.ACCEPTED: CommercialStage.ACCEPTED, RevisionStatus.DECLINED: CommercialStage.DECLINED,
            RevisionStatus.WITHDRAWN: CommercialStage.WITHDRAWN}.get(current.status, CommercialStage.CLOSED)


def next_action(profile: CommercialProfile, f: CommercialFacts) -> CommercialNextAction:
    ready = readiness(profile, f)
    blockers = list(dict.fromkeys([*ready.blockers, *ready.warnings]))
    current = f.current
    status = current.status if current else None
    if status is RevisionStatus.APPROVED:
        blockers.append(CommercialBlocker.PROPOSAL_NOT_PRESENTED)
    if status is RevisionStatus.DRAFT:
        blockers.append(CommercialBlocker.PROPOSAL_NOT_APPROVED)
    if revision_required(profile, f):
        blockers.append(CommercialBlocker.PROPOSAL_REVISION_REQUIRED)
    if f.open_signals(SignalKind.ACCEPTANCE):
        blockers.append(CommercialBlocker.ACCEPTANCE_SIGNAL)
    if f.open_signals(SignalKind.DECLINE):
        blockers.append(CommercialBlocker.DECLINE_SIGNAL)
    owner, action = _decide(profile, f, ready)
    return CommercialNextAction(owner=owner, action=action, blockers=tuple(dict.fromkeys(blockers)))


def _decide(profile: CommercialProfile, f: CommercialFacts, ready: ProposalReadiness) -> tuple[NextActionOwner, CommercialAction]:
    O, A = NextActionOwner, CommercialAction  # noqa: N806 - local aliases
    if f.lead.stage is LeadStage.CLOSED or f.opportunity.status not in ACTIVE_OPPORTUNITY_STATUSES:
        return O.NONE, A.CLOSED
    if f.suppressed:
        return O.NONE, A.NONE  # DNC: history stays readable, nothing progresses
    if f.open_signals(SignalKind.ACCEPTANCE):
        return O.OPERATOR, A.CONFIRM_ACCEPTANCE
    if f.open_signals(SignalKind.DECLINE):
        return O.OPERATOR, A.DECIDE_LOSS
    if f.open_requests:
        return O.OPERATOR, A.REVIEW_TERM_REQUEST
    if CommercialBlocker.QUALIFICATION_NOT_APPROVED in ready.blockers or CommercialBlocker.QUALIFICATION_CONFLICT in ready.blockers:
        return O.OPERATOR, A.COMPLETE_COMMERCIAL_INPUTS
    if revision_required(profile, f):
        return O.OPERATOR, A.REVIEW_REVISION
    current = f.current
    if current is None:
        return O.OPERATOR, A.PREPARE_PROPOSAL
    if current.status is RevisionStatus.DRAFT:
        return O.OPERATOR, (A.REVIEW_PROPOSAL if ready.level is ReadinessLevel.READY_FOR_REVIEW else A.COMPLETE_COMMERCIAL_INPUTS)
    if current.status is RevisionStatus.APPROVED:
        return O.OPERATOR, A.PRESENT_PROPOSAL
    if current.status is RevisionStatus.PRESENTED:
        if f.open_objections:
            return O.OPERATOR, A.HANDLE_OBJECTION
        return O.CUSTOMER, A.WAIT_FOR_CUSTOMER_DECISION
    if current.status is RevisionStatus.ACCEPTED:
        return O.OPERATOR, A.COMPLETE_WON
    if current.status is RevisionStatus.DECLINED:
        return O.OPERATOR, A.DECIDE_LOSS
    return O.OPERATOR, A.PREPARE_PROPOSAL  # withdrawn/superseded: a new revision is an explicit choice


def objection_open(objection: Objection) -> bool:
    return objection.status in (ObjectionStatus.OPEN, ObjectionStatus.ACKNOWLEDGED)
