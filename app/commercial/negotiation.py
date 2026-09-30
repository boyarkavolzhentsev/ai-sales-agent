"""Negotiation records from customer messages, and their operator handling.

``record_extraction`` applies one validated extraction of one customer message:
- requested terms become ``TermRequest`` rows (deduplicated per message and value): an
  agreeing value is ignored (already approved), a disagreeing one is UNDER_REVIEW (the
  approved value stays), one with nothing approved yet is REQUESTED; scope changes are
  IMPLEMENTATION_SCOPE requests; invalid shapes (e.g. an unknown currency) are ignored;
- objections become OPEN ``Objection`` rows (one per category per message);
- acceptance/decline become OPEN ``CommercialSignal`` rows; a newer message's signal
  supersedes older open ones, and an older message replayed later is recorded as
  already SUPERSEDED (it never outranks what the customer said since).
Nothing here approves a term, decides a proposal, or closes a lead. Objections are only
closed by an operator: sending a response never resolves one.
"""

from dataclasses import dataclass, field
from datetime import datetime

from app.commercial.config import TERM_KINDS, CommercialProfile
from app.commercial.contracts import CommercialExtraction, RequestedTerm
from app.commercial.errors import CommercialCode, CommercialError, CommercialNotFoundError
from app.commercial.guards import OpportunityContext
from app.commercial.proposals import close_signal
from app.commercial.terms import resolve
from app.core.enums import ObjectionStatus, RefKind, SignalKind, SignalStatus, TermRequestStatus, TermType, ValueKind
from app.core.models import (
    OPEN_OBJECTION_STATUSES,
    OPEN_REVISION_STATUSES,
    CommercialSignal,
    CommercialValue,
    EmailMessage,
    Objection,
    TermRequest,
)
from app.inbound.models import stable_id
from app.persistence import UnitOfWork
from app.pipeline.audit import operator_actor, record_event, ref


@dataclass
class ExtractionOutcome:
    requests: list[str] = field(default_factory=list)
    objections: list[str] = field(default_factory=list)
    signals: list[str] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)


def record_extraction(
    uow: UnitOfWork, profile: CommercialProfile, context: OpportunityContext, message: EmailMessage,
    extraction: CommercialExtraction, *, correlation_id: str, now: datetime,
) -> ExtractionOutcome:
    outcome = ExtractionOutcome()
    opportunity_id = context.opportunity.opportunity_id
    revisions = uow.proposal_revisions.list_for_opportunity(opportunity_id)
    current = next((r for r in revisions if r.status in OPEN_REVISION_STATUSES), revisions[-1] if revisions else None)
    terms = uow.commercial_terms.list_for_opportunity(opportunity_id)
    wanted = [*extraction.requested_terms, *(
        RequestedTerm(term_type=TermType.IMPLEMENTATION_SCOPE, value=CommercialValue(kind=ValueKind.TEXT, text=change))
        for change in extraction.scope_changes)]
    for requested in wanted:
        if requested.value.kind is not TERM_KINDS[requested.term_type] or (
                requested.value.money is not None and requested.value.money.currency not in profile.currencies):
            outcome.ignored.append(f"{requested.term_type.value}:INVALID_VALUE")
            continue
        approved = resolve(profile, current, terms, requested.term_type, requested.term_key, now)
        if approved is not None and approved.value.same_as(requested.value):
            outcome.ignored.append(f"{requested.term_type.value}:ALREADY_APPROVED")
            continue
        request_id = stable_id("tr", opportunity_id, requested.term_type.value, requested.term_key,
                               requested.value.display().casefold(), message.message_id)
        if uow.term_requests.get(request_id) is not None:
            continue
        request = TermRequest(
            request_id=request_id, opportunity_id=opportunity_id, lead_id=context.lead.lead_id,
            term_type=requested.term_type, term_key=requested.term_key, requested_value=requested.value,
            approved_value_at_request=approved.value if approved else None, evidence_message_id=message.message_id,
            status=TermRequestStatus.UNDER_REVIEW if approved else TermRequestStatus.REQUESTED,
            created_at=now, updated_at=now)
        uow.term_requests.add(request)
        record_event(uow, key=(request_id, "1"), event_type="TERM_REQUESTED",
                     subjects=(ref(RefKind.TERM_REQUEST, request_id), ref(RefKind.OPPORTUNITY, opportunity_id),
                               ref(RefKind.EMAIL_MESSAGE, message.message_id)),
                     after={"term_type": request.term_type.value, "requested": request.requested_value.display(),
                            "approved": approved.value.display() if approved else None, "status": request.status.value},
                     correlation_id=correlation_id, now=now)
        outcome.requests.append(request_id)
    for proposal in extraction.objections:
        objection_id = stable_id("ob", opportunity_id, proposal.category.value, message.message_id)
        if uow.objections.get(objection_id) is not None:
            continue
        uow.objections.add(Objection(objection_id=objection_id, opportunity_id=opportunity_id, lead_id=context.lead.lead_id,
                                     category=proposal.category, summary=proposal.summary, source_message_id=message.message_id,
                                     created_at=now, updated_at=now))
        record_event(uow, key=(objection_id, "1"), event_type="OBJECTION_RECORDED",
                     subjects=(ref(RefKind.OBJECTION, objection_id), ref(RefKind.OPPORTUNITY, opportunity_id),
                               ref(RefKind.EMAIL_MESSAGE, message.message_id)),
                     after={"category": proposal.category.value, "summary": proposal.summary},
                     correlation_id=correlation_id, now=now)
        outcome.objections.append(objection_id)
    kinds = [k for k, flag in ((SignalKind.ACCEPTANCE, extraction.acceptance_signal),
                               (SignalKind.DECLINE, extraction.decline_signal)) if flag]
    for kind in kinds:
        signal_id = _record_signal(uow, context, current.revision_id if current else None, kind, message,
                                   correlation_id=correlation_id, now=now)
        if signal_id is not None:
            outcome.signals.append(signal_id)
    return outcome


def _record_signal(uow: UnitOfWork, context: OpportunityContext, revision_id: str | None, kind: SignalKind,
                   message: EmailMessage, *, correlation_id: str, now: datetime) -> str | None:
    opportunity_id = context.opportunity.opportunity_id
    signal_id = stable_id("cs", opportunity_id, kind.value, message.message_id)
    if uow.commercial_signals.get(signal_id) is not None:
        return None
    message_at = message.received_at or now
    existing = uow.commercial_signals.list_for_opportunity(opportunity_id)
    newer = any(s.message_at > message_at for s in existing)
    status = SignalStatus.SUPERSEDED if newer else SignalStatus.OPEN
    signal = CommercialSignal(
        signal_id=signal_id, opportunity_id=opportunity_id, lead_id=context.lead.lead_id, revision_id=revision_id,
        kind=kind, source_message_id=message.message_id, message_at=message_at, status=status,
        resolved_at=now if newer else None, created_at=now, updated_at=now)
    uow.commercial_signals.add(signal)
    record_event(uow, key=(signal_id, "1"), event_type=f"COMMERCIAL_SIGNAL_{kind.value}",
                 subjects=(ref(RefKind.COMMERCIAL_SIGNAL, signal_id), ref(RefKind.OPPORTUNITY, opportunity_id),
                           ref(RefKind.EMAIL_MESSAGE, message.message_id)),
                 after={"kind": kind.value, "status": status.value, "revision_id": revision_id},
                 correlation_id=correlation_id, now=now)
    if not newer:
        for older in existing:
            if older.status is SignalStatus.OPEN and older.message_at < message_at:
                close_signal(uow, older, SignalStatus.SUPERSEDED, operator_id=None, correlation_id=correlation_id, now=now)
    return signal_id


def update_objection(uow: UnitOfWork, *, objection_id: str, expected_version: int, status: ObjectionStatus,
                     resolution: str | None, operator_id: str, correlation_id: str, now: datetime) -> Objection:
    objection = uow.objections.get(objection_id)
    if objection is None:
        raise CommercialNotFoundError(f"objection {objection_id} not found")
    if objection.version != expected_version:
        raise CommercialError(CommercialCode.OBJECTION_VERSION_CHANGED)
    if objection.status not in OPEN_OBJECTION_STATUSES or status is ObjectionStatus.OPEN or status is objection.status:
        raise CommercialError(CommercialCode.OBJECTION_NOT_OPEN)
    closing = status not in OPEN_OBJECTION_STATUSES
    updated = Objection.model_validate(objection.model_dump() | {
        "status": status, "resolution": resolution, "resolved_by": operator_id if closing else None,
        "resolved_at": now if closing else None, "updated_at": max(now, objection.updated_at),
        "version": objection.version + 1})
    uow.objections.update(updated, objection.version)
    record_event(uow, key=(objection_id, str(updated.version)), event_type=f"OBJECTION_{status.value}",
                 subjects=(ref(RefKind.OBJECTION, objection_id), ref(RefKind.OPPORTUNITY, objection.opportunity_id)),
                 before={"status": objection.status.value}, after={"status": status.value, "resolution": resolution},
                 actor=operator_actor(operator_id), correlation_id=correlation_id, now=now)
    return updated


def dismiss_signal(uow: UnitOfWork, *, signal_id: str, expected_version: int, operator_id: str, correlation_id: str,
                   now: datetime) -> CommercialSignal:
    signal = uow.commercial_signals.get(signal_id)
    if signal is None:
        raise CommercialNotFoundError(f"signal {signal_id} not found")
    if signal.version != expected_version:
        raise CommercialError(CommercialCode.SIGNAL_VERSION_CHANGED)
    if signal.status is not SignalStatus.OPEN:
        raise CommercialError(CommercialCode.SIGNAL_NOT_OPEN)
    return close_signal(uow, signal, SignalStatus.DISMISSED, operator_id=operator_id, correlation_id=correlation_id, now=now)


def cancel_open_signals(uow: UnitOfWork, opportunity_id: str, *, correlation_id: str, now: datetime) -> int:
    count = 0
    for signal in uow.commercial_signals.list_for_opportunity(opportunity_id):
        if signal.status is SignalStatus.OPEN:
            close_signal(uow, signal, SignalStatus.CANCELLED, operator_id=None, correlation_id=correlation_id, now=now)
            count += 1
    return count
