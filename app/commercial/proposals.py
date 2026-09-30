"""Proposals: one per opportunity, as a chain of revisions. Operator actions only.

- create: revision 1 as a DRAFT, only for an active opportunity of an open, unsuppressed
  lead with an approved (conflict-free) qualification, in an allowed currency.
- update_draft: only a DRAFT's content changes. A line price comes from the operator's
  command or from an approved internal knowledge fact in the proposal's currency; never
  converted, never guessed. Line discounts are operator values within the policy.
- approve: only when readiness is READY_FOR_REVIEW. Freezes the resolved terms and the
  Decimal totals into the revision; from then on its content never changes.
- present: explicit operator confirmation that it was communicated (a draft, an
  approval or an unconfirmed send never counts), refused under DNC or when the revision
  no longer matches what is approved.
- revise: the next revision copies the latest one; an APPROVED/PRESENTED predecessor
  becomes SUPERSEDED (content kept). Revision numbers only increase; SQL allows one open
  revision per proposal and unique (proposal, revision) pairs.
- accept / decline: an operator records the customer's decision on the PRESENTED
  revision. Neither closes the lead: WON and LOST stay Stage 12 operator commands.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from pydantic import JsonValue

from app.commercial.config import CommercialConfig
from app.commercial.errors import CommercialCode, CommercialError, CommercialNotFoundError
from app.commercial.guards import OpportunityContext, load_opportunity, require_active
from app.commercial.pricing import PriceCatalog
from app.commercial.state import ReadinessLevel, draft_totals, gather, readiness, revision_required
from app.commercial.terms import check_discount, effective_terms, validate
from app.core.enums import QualificationStatus, RefKind, RevisionStatus, SignalKind, SignalStatus, TermSource, TermType
from app.core.models import (
    OPEN_REVISION_STATUSES,
    AppliedTerm,
    CommercialSignal,
    CommercialValue,
    Money,
    ProposalLine,
    ProposalRevision,
    ValueSource,
)
from app.core.models.commercial import MAIN_KEY
from app.inbound.models import stable_id
from app.persistence import AlreadyExistsError, IntegrityError, UnitOfWork
from app.pipeline.audit import PIPELINE_ACTOR, operator_actor, record_event, ref


@dataclass(frozen=True)
class LineInput:
    line_id: str
    item_ref: str
    quantity: Decimal
    unit: str
    description: str | None = None
    unit_price: Money | None = None
    discount_percent: Decimal | None = None


@dataclass(frozen=True)
class TermInput:
    term_type: TermType
    value: CommercialValue
    term_key: str = MAIN_KEY


def proposal_id_for(opportunity_id: str) -> str:
    return stable_id("pp", opportunity_id)


def revision_id_for(proposal_id: str, number: int) -> str:
    return stable_id("pv", proposal_id, str(number))


def load_revision(uow: UnitOfWork, revision_id: str, expected_version: int) -> tuple[ProposalRevision, OpportunityContext]:
    revision = uow.proposal_revisions.get(revision_id)
    if revision is None:
        raise CommercialNotFoundError(f"proposal revision {revision_id} not found")
    if revision.version != expected_version:
        raise CommercialError(CommercialCode.REVISION_VERSION_CHANGED)
    return revision, load_opportunity(uow, revision.opportunity_id)


def create(
    uow: UnitOfWork, config: CommercialConfig, *, opportunity_id: str, expected_opportunity_version: int, currency: str,
    operator_id: str, command_id: str, correlation_id: str, now: datetime,
) -> ProposalRevision:
    context = load_opportunity(uow, opportunity_id)
    if context.opportunity.version != expected_opportunity_version:
        raise CommercialError(CommercialCode.OPPORTUNITY_VERSION_CHANGED)
    require_active(uow, context, now)
    qualification = uow.qualifications.get(context.lead.lead_id)
    if qualification is None or qualification.status is not QualificationStatus.QUALIFIED or qualification.open_conflicts:
        raise CommercialError(CommercialCode.QUALIFICATION_NOT_APPROVED)
    if currency not in config.profile.currencies:
        raise CommercialError(CommercialCode.CURRENCY_NOT_ALLOWED)
    if uow.proposal_revisions.list_for_opportunity(opportunity_id):
        raise CommercialError(CommercialCode.PROPOSAL_EXISTS)
    proposal_id = proposal_id_for(opportunity_id)
    revision = ProposalRevision(
        revision_id=revision_id_for(proposal_id, 1), proposal_id=proposal_id, opportunity_id=opportunity_id,
        lead_id=context.lead.lead_id, revision=1, currency=currency, created_by=operator_id, created_at=now, updated_at=now)
    _add(uow, revision)
    _audit(uow, revision, "PROPOSAL_CREATED", None, operator_id, correlation_id, now)
    return revision


def update_draft(
    uow: UnitOfWork, config: CommercialConfig, catalog: PriceCatalog, *, revision_id: str, expected_version: int,
    lines: tuple[LineInput, ...], term_overrides: tuple[TermInput, ...], assumptions: tuple[str, ...],
    exclusions: tuple[str, ...], next_step: str | None, operator_id: str, command_id: str, correlation_id: str, now: datetime,
) -> ProposalRevision:
    revision, context = load_revision(uow, revision_id, expected_version)
    if revision.status is not RevisionStatus.DRAFT:
        raise CommercialError(CommercialCode.REVISION_NOT_EDITABLE)
    require_active(uow, context, now)
    profile = config.profile
    operator_source = ValueSource(source=TermSource.OPERATOR, operator_id=operator_id, command_id=command_id, recorded_at=now)
    built: list[ProposalLine] = []
    for line in lines:
        price, price_source = None, None
        if line.unit_price is not None:
            if line.unit_price.currency != revision.currency:
                raise CommercialError(CommercialCode.CURRENCY_MISMATCH)
            price, price_source = line.unit_price, operator_source
        else:
            fact = catalog.price_for(uow, line.item_ref, now)
            if fact is not None and fact.currency == revision.currency:  # never converted
                price = Money(amount=fact.amount, currency=fact.currency)
                price_source = ValueSource(source=TermSource.KNOWLEDGE, knowledge_source_id=fact.source_id,
                                           knowledge_source_version=fact.source_version, fact_key=fact.fact_key,
                                           recorded_at=now)
        if line.discount_percent is not None:
            check_discount(profile, line.discount_percent, item_ref=line.item_ref)
        built.append(ProposalLine(
            line_id=line.line_id, item_ref=line.item_ref, description=line.description, quantity=line.quantity,
            unit=line.unit, unit_price=price, price_source=price_source, discount_percent=line.discount_percent,
            discount_source=operator_source if line.discount_percent is not None else None))
    overrides: list[AppliedTerm] = []
    override_source = operator_source.model_copy(update={"source": TermSource.REVISION_OVERRIDE})
    for term in term_overrides:
        validate(profile, term.term_type, term.value, currency=revision.currency)
        overrides.append(AppliedTerm(term_type=term.term_type, term_key=term.term_key, value=term.value,
                                     provenance=override_source))
    updated = ProposalRevision.model_validate(revision.model_dump() | {
        "lines": built, "term_overrides": overrides, "assumptions": assumptions, "exclusions": exclusions,
        "next_step": next_step, "updated_at": max(now, revision.updated_at), "version": revision.version + 1})
    uow.proposal_revisions.update(updated, revision.version)
    _audit(uow, updated, "PROPOSAL_DRAFT_UPDATED", revision, operator_id, correlation_id, now,
           extra={"items": list[JsonValue](line.item_ref for line in built),
                  "priced": sum(1 for line in built if line.unit_price)})
    return updated


def approve(
    uow: UnitOfWork, config: CommercialConfig, *, revision_id: str, expected_version: int, operator_id: str,
    correlation_id: str, now: datetime,
) -> ProposalRevision:
    revision, context = load_revision(uow, revision_id, expected_version)
    if revision.status is not RevisionStatus.DRAFT:
        raise CommercialError(CommercialCode.REVISION_STATUS_INVALID)
    require_active(uow, context, now)
    facts = gather(uow, context.opportunity, now)
    ready = readiness(config.profile, facts)
    if ready.level is not ReadinessLevel.READY_FOR_REVIEW:
        raise CommercialError(CommercialCode.PROPOSAL_NOT_READY, *(b.value for b in ready.blockers))
    totals = draft_totals(config.profile, facts, revision)
    if totals is None:
        raise CommercialError(CommercialCode.PROPOSAL_NOT_READY)
    frozen = effective_terms(config.profile, revision, list(facts.terms), now)
    approved = ProposalRevision.model_validate(revision.model_dump() | {
        "status": RevisionStatus.APPROVED, "frozen_terms": frozen, "totals": totals, "approved_by": operator_id,
        "approved_at": now, "updated_at": max(now, revision.updated_at), "version": revision.version + 1})
    uow.proposal_revisions.update(approved, revision.version)
    _audit(uow, approved, "PROPOSAL_APPROVED", revision, operator_id, correlation_id, now,
           extra={"total": str(totals.total.amount), "currency": totals.currency})
    return approved


def present(uow: UnitOfWork, config: CommercialConfig, *, revision_id: str, expected_version: int, operator_id: str,
            correlation_id: str, now: datetime) -> ProposalRevision:
    revision, context = load_revision(uow, revision_id, expected_version)
    if revision.status is not RevisionStatus.APPROVED:
        raise CommercialError(CommercialCode.REVISION_STATUS_INVALID)
    require_active(uow, context, now)  # DNC: nothing is communicated
    if revision_required(config.profile, gather(uow, context.opportunity, now)):
        raise CommercialError(CommercialCode.PROPOSAL_NOT_READY, "PROPOSAL_REVISION_REQUIRED")
    return _move(uow, revision, RevisionStatus.PRESENTED, operator_id, correlation_id, now, presented_at=now)


def revise(uow: UnitOfWork, *, revision_id: str, expected_version: int, operator_id: str, correlation_id: str,
           now: datetime) -> ProposalRevision:
    revision, context = load_revision(uow, revision_id, expected_version)
    require_active(uow, context, now)
    latest = uow.proposal_revisions.latest_for_opportunity(revision.opportunity_id)
    if latest is None or latest.revision_id != revision.revision_id:
        raise CommercialError(CommercialCode.REVISION_NOT_CURRENT)
    if revision.status in (RevisionStatus.DRAFT, RevisionStatus.ACCEPTED, RevisionStatus.CLOSED):
        raise CommercialError(CommercialCode.REVISION_STATUS_INVALID)
    if revision.status in OPEN_REVISION_STATUSES:
        _move(uow, revision, RevisionStatus.SUPERSEDED, operator_id, correlation_id, now)
    number = revision.revision + 1
    successor = ProposalRevision(
        revision_id=revision_id_for(revision.proposal_id, number), proposal_id=revision.proposal_id,
        opportunity_id=revision.opportunity_id, lead_id=revision.lead_id, revision=number,
        predecessor_id=revision.revision_id, currency=revision.currency, lines=revision.lines,
        term_overrides=revision.term_overrides, assumptions=revision.assumptions, exclusions=revision.exclusions,
        next_step=revision.next_step, created_by=operator_id, created_at=now, updated_at=now)
    _add(uow, successor)
    _audit(uow, successor, "PROPOSAL_REVISED", revision, operator_id, correlation_id, now)
    return successor


def withdraw(uow: UnitOfWork, *, revision_id: str, expected_version: int, reason: str, operator_id: str,
             correlation_id: str, now: datetime) -> ProposalRevision:
    revision, _ = load_revision(uow, revision_id, expected_version)
    if revision.status not in OPEN_REVISION_STATUSES:
        raise CommercialError(CommercialCode.REVISION_STATUS_INVALID)
    return _move(uow, revision, RevisionStatus.WITHDRAWN, operator_id, correlation_id, now, decided=True, reason=reason)


def decide(uow: UnitOfWork, *, revision_id: str, expected_version: int, accepted: bool, reason: str | None,
           operator_id: str, correlation_id: str, now: datetime) -> ProposalRevision:
    """The operator records the customer's decision on the presented revision. The lead
    stays open either way; matching open signals are confirmed, contrary ones superseded."""
    revision, context = load_revision(uow, revision_id, expected_version)
    if revision.status is not RevisionStatus.PRESENTED:
        raise CommercialError(CommercialCode.REVISION_STATUS_INVALID)
    require_active(uow, context, now, allow_suppressed=True)  # recording the customer's own decision
    status = RevisionStatus.ACCEPTED if accepted else RevisionStatus.DECLINED
    decided = _move(uow, revision, status, operator_id, correlation_id, now, decided=True, reason=reason)
    matching = SignalKind.ACCEPTANCE if accepted else SignalKind.DECLINE
    for signal in uow.commercial_signals.list_for_opportunity(revision.opportunity_id):
        if signal.status is SignalStatus.OPEN:
            close_signal(uow, signal, SignalStatus.CONFIRMED if signal.kind is matching else SignalStatus.SUPERSEDED,
                         operator_id=operator_id, correlation_id=correlation_id, now=now)
    return decided


def close_signal(uow: UnitOfWork, signal: CommercialSignal, status: SignalStatus, *, operator_id: str | None,
                 correlation_id: str, now: datetime) -> CommercialSignal:
    closed = CommercialSignal.model_validate(signal.model_dump() | {
        "status": status, "resolved_by": operator_id, "resolved_at": now, "updated_at": max(now, signal.updated_at),
        "version": signal.version + 1})
    uow.commercial_signals.update(closed, signal.version)
    record_event(uow, key=(signal.signal_id, str(closed.version)), event_type=f"COMMERCIAL_SIGNAL_{status.value}",
                 subjects=(ref(RefKind.COMMERCIAL_SIGNAL, signal.signal_id), ref(RefKind.OPPORTUNITY, signal.opportunity_id)),
                 before={"status": signal.status.value}, after={"status": status.value, "kind": signal.kind.value},
                 actor=operator_actor(operator_id) if operator_id else PIPELINE_ACTOR, correlation_id=correlation_id, now=now)
    return closed


def close_open_revisions(uow: UnitOfWork, opportunity_id: str, *, correlation_id: str, now: datetime) -> int:
    count = 0
    for revision in uow.proposal_revisions.list_for_opportunity(opportunity_id):
        if revision.status in OPEN_REVISION_STATUSES:
            _move(uow, revision, RevisionStatus.CLOSED, None, correlation_id, now)
            count += 1
    return count


def _move(uow: UnitOfWork, revision: ProposalRevision, status: RevisionStatus, operator_id: str | None,
          correlation_id: str, now: datetime, *, presented_at: datetime | None = None, decided: bool = False,
          reason: str | None = None) -> ProposalRevision:
    changes: dict[str, object] = {"status": status, "updated_at": max(now, revision.updated_at), "version": revision.version + 1}
    if presented_at is not None:
        changes["presented_at"] = presented_at
    if decided:
        changes |= {"decided_by": operator_id, "decided_at": now, "decision_reason": reason}
    moved = ProposalRevision.model_validate(revision.model_dump() | changes)
    uow.proposal_revisions.update(moved, revision.version)
    _audit(uow, moved, f"PROPOSAL_{status.value}", revision, operator_id, correlation_id, now,
           extra={"reason": reason} if reason else None)
    return moved


def _add(uow: UnitOfWork, revision: ProposalRevision) -> None:
    try:
        uow.proposal_revisions.add(revision)
    except (AlreadyExistsError, IntegrityError):  # SQL: unique revision identity, one open revision
        raise CommercialError(CommercialCode.PROPOSAL_EXISTS if revision.revision == 1
                              else CommercialCode.REVISION_NOT_CURRENT) from None


def _audit(uow: UnitOfWork, after: ProposalRevision, event_type: str, before: ProposalRevision | None,
           operator_id: str | None, correlation_id: str, now: datetime, *, extra: dict[str, JsonValue] | None = None) -> None:
    state: dict[str, JsonValue] = {"status": after.status.value, "revision": after.revision, "version": after.version,
                                "lines": len(after.lines)} | (extra or {})
    record_event(uow, key=(after.revision_id, str(after.version)), event_type=event_type,
                 subjects=(ref(RefKind.PROPOSAL_REVISION, after.revision_id), ref(RefKind.OPPORTUNITY, after.opportunity_id),
                           ref(RefKind.LEAD, after.lead_id)),
                 before={"status": before.status.value, "version": before.version} if before else None,
                 after=state, actor=operator_actor(operator_id) if operator_id else PIPELINE_ACTOR,
                 correlation_id=correlation_id, now=now)
