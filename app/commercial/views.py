"""Operator-facing commercial read models, queues and metrics (no message bodies).

Money is never aggregated across currencies: metrics group values by currency and no
conversion rate exists anywhere.
"""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import AwareDatetime

from app.commercial.config import CommercialProfile
from app.commercial.state import (
    CommercialFacts,
    CommercialNextAction,
    ProposalReadiness,
    gather,
    next_action,
    readiness,
    revision_required,
    stage,
)
from app.core.enums import (
    CommercialAction,
    CommercialStage,
    NextActionOwner,
    ObjectionStatus,
    OpportunityStatus,
    RevisionStatus,
    SignalKind,
    TermRequestStatus,
)
from app.core.models import Money, Opportunity, ProposalRevision
from app.core.models.base import CoreModel
from app.core.models.types import EntityId
from app.persistence import UnitOfWork
from app.pipeline.policy import OPEN_STAGES


class CommercialView(CoreModel):
    lead_id: EntityId
    opportunity_id: EntityId
    opportunity_status: OpportunityStatus
    opportunity_version: int
    stage: CommercialStage
    proposal_id: EntityId | None = None
    revision_id: EntityId | None = None
    revision: int | None = None
    revision_status: RevisionStatus | None = None
    revision_version: int | None = None
    readiness: ProposalReadiness
    approved_total: Money | None = None  # frozen at approval; never computed from requests
    currency: str | None = None
    open_request_ids: tuple[EntityId, ...] = ()
    open_objection_ids: tuple[EntityId, ...] = ()
    open_signal_ids: tuple[EntityId, ...] = ()
    suppressed: bool
    next_action: CommercialNextAction
    last_commercial_activity: AwareDatetime


class CommercialQueue(StrEnum):
    NEEDS_COMMERCIAL_INPUT = "NEEDS_COMMERCIAL_INPUT"
    NEEDS_TERM_REVIEW = "NEEDS_TERM_REVIEW"
    NEEDS_PROPOSAL_REVIEW = "NEEDS_PROPOSAL_REVIEW"
    READY_TO_PRESENT = "READY_TO_PRESENT"
    WAITING_FOR_CUSTOMER = "WAITING_FOR_CUSTOMER"
    HAS_OPEN_OBJECTIONS = "HAS_OPEN_OBJECTIONS"
    NEEDS_REVISION = "NEEDS_REVISION"
    ACCEPTANCE_SIGNAL_REVIEW = "ACCEPTANCE_SIGNAL_REVIEW"
    DECLINE_SIGNAL_REVIEW = "DECLINE_SIGNAL_REVIEW"


class CurrencyAmount(CoreModel):
    currency: str
    amount: Decimal
    proposals: int


class CommercialMetrics(CoreModel):
    revisions_by_status: dict[RevisionStatus, int]
    revisions_total: int
    opportunities_with_proposal: int
    opportunities_without_proposal: int  # among active opportunities
    open_term_requests: int
    resolved_term_requests: int
    open_objections: int
    accepted_proposals: int
    declined_proposals: int
    proposed_value_by_currency: tuple[CurrencyAmount, ...]  # approved/presented/accepted current revisions
    average_discount_percent: Decimal | None  # only over revisions whose frozen discount is known
    discounted_revisions: int


def view_of(profile: CommercialProfile, facts: CommercialFacts) -> CommercialView:
    current = facts.current
    activity = [facts.opportunity.updated_at, *(r.updated_at for r in facts.revisions),
                *(r.updated_at for r in facts.requests), *(o.updated_at for o in facts.objections),
                *(s.updated_at for s in facts.signals)]
    return CommercialView(
        lead_id=facts.lead.lead_id, opportunity_id=facts.opportunity.opportunity_id,
        opportunity_status=facts.opportunity.status, opportunity_version=facts.opportunity.version,
        stage=stage(profile, facts), proposal_id=current.proposal_id if current else None,
        revision_id=current.revision_id if current else None, revision=current.revision if current else None,
        revision_status=current.status if current else None, revision_version=current.version if current else None,
        readiness=readiness(profile, facts), approved_total=current.totals.total if current and current.totals else None,
        currency=current.currency if current else None,
        open_request_ids=tuple(r.request_id for r in facts.open_requests),
        open_objection_ids=tuple(o.objection_id for o in facts.open_objections),
        open_signal_ids=tuple(s.signal_id for k in SignalKind for s in facts.open_signals(k)),
        suppressed=facts.suppressed, next_action=next_action(profile, facts), last_commercial_activity=max(activity),
    )


def opportunity_view(uow: UnitOfWork, profile: CommercialProfile, opportunity: Opportunity, now: datetime) -> CommercialView:
    return view_of(profile, gather(uow, opportunity, now))


def queue(uow: UnitOfWork, profile: CommercialProfile, which: CommercialQueue, now: datetime,
          limit: int) -> tuple[CommercialView, ...]:
    views: list[CommercialView] = []
    for lead in uow.leads.list_by_stages(list(OPEN_STAGES), limit):
        opportunity = uow.opportunities.get_active_for_lead(lead.lead_id)
        if opportunity is None:
            continue
        facts = gather(uow, opportunity, now)
        view = view_of(profile, facts)
        if _matches(profile, which, facts, view):
            views.append(view)
    return tuple(sorted(views, key=lambda v: (v.last_commercial_activity, v.opportunity_id)))


def _matches(profile: CommercialProfile, which: CommercialQueue, facts: CommercialFacts, view: CommercialView) -> bool:
    action = view.next_action.action
    if which is CommercialQueue.NEEDS_COMMERCIAL_INPUT:
        return action in (CommercialAction.COMPLETE_COMMERCIAL_INPUTS, CommercialAction.PREPARE_PROPOSAL)
    if which is CommercialQueue.NEEDS_TERM_REVIEW:
        return bool(view.open_request_ids)
    if which is CommercialQueue.NEEDS_PROPOSAL_REVIEW:
        return action is CommercialAction.REVIEW_PROPOSAL
    if which is CommercialQueue.READY_TO_PRESENT:
        return action is CommercialAction.PRESENT_PROPOSAL
    if which is CommercialQueue.WAITING_FOR_CUSTOMER:
        return view.next_action.owner is NextActionOwner.CUSTOMER
    if which is CommercialQueue.HAS_OPEN_OBJECTIONS:
        return bool(view.open_objection_ids)
    if which is CommercialQueue.NEEDS_REVISION:
        return revision_required(profile, facts)
    if which is CommercialQueue.ACCEPTANCE_SIGNAL_REVIEW:
        return bool(facts.open_signals(SignalKind.ACCEPTANCE))
    return bool(facts.open_signals(SignalKind.DECLINE))  # DECLINE_SIGNAL_REVIEW


def metrics(uow: UnitOfWork) -> CommercialMetrics:
    revisions = uow.proposal_revisions.list_by_status(list(RevisionStatus))
    by_status: dict[RevisionStatus, int] = {}
    latest: dict[str, ProposalRevision] = {}
    for revision in revisions:
        by_status[revision.status] = by_status.get(revision.status, 0) + 1
        if revision.proposal_id not in latest or revision.revision > latest[revision.proposal_id].revision:
            latest[revision.proposal_id] = revision
    valued: dict[str, list[Decimal]] = {}
    discounts: list[Decimal] = []
    for revision in latest.values():  # each proposal counted once, at its current revision
        if revision.totals is None:
            continue
        if revision.status in (RevisionStatus.APPROVED, RevisionStatus.PRESENTED, RevisionStatus.ACCEPTED):
            valued.setdefault(revision.totals.currency, []).append(revision.totals.total.amount)
        if revision.totals.discount_percent is not None:
            discounts.append(revision.totals.discount_percent)
    requests = uow.term_requests.count_by_status()
    open_requests = requests.get(TermRequestStatus.REQUESTED, 0) + requests.get(TermRequestStatus.UNDER_REVIEW, 0)
    objections = uow.objections.count_by_status()
    active = {o.opportunity_id for o in _active_opportunities(uow)}
    with_proposal = {r.opportunity_id for r in revisions}
    return CommercialMetrics(
        revisions_by_status=by_status, revisions_total=len(revisions),
        opportunities_with_proposal=len(active & with_proposal), opportunities_without_proposal=len(active - with_proposal),
        open_term_requests=open_requests, resolved_term_requests=sum(requests.values()) - open_requests,
        open_objections=objections.get(ObjectionStatus.OPEN, 0) + objections.get(ObjectionStatus.ACKNOWLEDGED, 0),
        accepted_proposals=by_status.get(RevisionStatus.ACCEPTED, 0), declined_proposals=by_status.get(RevisionStatus.DECLINED, 0),
        proposed_value_by_currency=tuple(CurrencyAmount(currency=c, amount=sum(v, Decimal(0)), proposals=len(v))
                                         for c, v in sorted(valued.items())),
        average_discount_percent=(sum(discounts, Decimal(0)) / len(discounts)).quantize(Decimal("0.01")) if discounts else None,
        discounted_revisions=len(discounts),
    )


def _active_opportunities(uow: UnitOfWork) -> list[Opportunity]:
    found = [uow.opportunities.get_active_for_lead(lead.lead_id) for lead in uow.leads.list_by_stages(list(OPEN_STAGES), 1_000_000)]
    return [o for o in found if o is not None]
