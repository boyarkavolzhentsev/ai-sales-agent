"""Approved commercial terms, their precedence, and customer term requests.

Precedence (first match wins; customer requests never take part):
  1. the proposal revision's approved value (an operator override on a draft revision,
     or the value frozen into an approved/presented/accepted revision)
  2. the opportunity's approved, opportunity-specific term (operator-set, or an operator
     approving a customer's request)
  3. the commercial profile's explicitly configured default
  4. unknown
Opportunity-specific approvals never modify the global profile.

A customer's request is recorded as a ``TermRequest`` (REQUESTED, or UNDER_REVIEW when it
disagrees with an approved value, which stays in force). Only an operator approves or
rejects it; approving sets the opportunity term from the request (provenance: the
request and the approving command) and supersedes the term's other open requests.
"""

from datetime import datetime
from decimal import Decimal

from pydantic import JsonValue

from app.commercial.config import TERM_KINDS, CommercialProfile
from app.commercial.errors import CommercialCode, CommercialError, CommercialNotFoundError
from app.commercial.guards import OpportunityContext, load_opportunity, require_active
from app.core.enums import RefKind, RevisionStatus, TermRequestStatus, TermSource, TermType
from app.core.models import (
    OPEN_REQUEST_STATUSES,
    AppliedTerm,
    CommercialTerm,
    CommercialValue,
    ProposalRevision,
    TermRequest,
    ValueSource,
)
from app.core.models.commercial import MAIN_KEY
from app.inbound.models import stable_id
from app.persistence import UnitOfWork
from app.pipeline.audit import PIPELINE_ACTOR, operator_actor, record_event, ref


def term_row_id(opportunity_id: str, term_type: TermType, term_key: str = MAIN_KEY) -> str:
    return stable_id("ct", opportunity_id, term_type.value, term_key)


def resolve(profile: CommercialProfile, revision: ProposalRevision | None, opportunity_terms: list[CommercialTerm],
            term_type: TermType, term_key: str, now: datetime) -> AppliedTerm | None:
    if revision is not None:
        layer = revision.term_overrides if revision.status is RevisionStatus.DRAFT else revision.frozen_terms
        found = next((t for t in layer if (t.term_type, t.term_key) == (term_type, term_key)), None)
        if found is not None:
            return found
        if revision.status is not RevisionStatus.DRAFT:
            return None  # a frozen revision means exactly what was approved
    term = next((t for t in opportunity_terms if (t.term_type, t.term_key) == (term_type, term_key)), None)
    if term is not None:
        return AppliedTerm(term_type=term_type, term_key=term_key, value=term.value, provenance=term.provenance)
    default = profile.term_defaults.get(term_type) if term_key == MAIN_KEY else None
    if default is not None:
        return AppliedTerm(term_type=term_type, value=default,
                           provenance=ValueSource(source=TermSource.PROFILE_DEFAULT, recorded_at=now))
    return None


def effective_terms(profile: CommercialProfile, revision: ProposalRevision | None,
                    opportunity_terms: list[CommercialTerm], now: datetime) -> tuple[AppliedTerm, ...]:
    keys: set[tuple[TermType, str]] = {(t.term_type, t.term_key) for t in opportunity_terms}
    keys |= {(t, MAIN_KEY) for t in profile.term_defaults}
    if revision is not None:
        keys |= {(t.term_type, t.term_key) for t in (*revision.term_overrides, *revision.frozen_terms)}
    resolved = [resolve(profile, revision, opportunity_terms, t, k, now) for t, k in sorted(keys)]
    return tuple(r for r in resolved if r is not None)


def validate(profile: CommercialProfile, term_type: TermType, value: CommercialValue, *, currency: str | None = None) -> None:
    """Shape and policy of an approved value. Raises CommercialError.

    PRICE and CURRENCY are never approved as terms: prices live on proposal lines (with
    their own provenance) and the currency is the proposal's. A term-level price would
    let a proposal claim a price its totals do not contain."""
    if term_type in (TermType.PRICE, TermType.CURRENCY):
        raise CommercialError(CommercialCode.TERM_VALUE_INVALID)
    if value.kind is not TERM_KINDS[term_type]:
        raise CommercialError(CommercialCode.TERM_VALUE_INVALID)
    if value.money is not None:
        if value.money.currency not in profile.currencies:
            raise CommercialError(CommercialCode.CURRENCY_NOT_ALLOWED)
        if currency is not None and value.money.currency != currency:
            raise CommercialError(CommercialCode.CURRENCY_MISMATCH)
    if term_type is TermType.DISCOUNT and value.percent is not None:
        check_discount(profile, value.percent, item_ref=None)


def check_discount(profile: CommercialProfile, percent: Decimal, *, item_ref: str | None) -> None:
    policy = profile.discount_policy
    if policy is None:
        return  # no configured limit: still an explicit operator decision, never automatic
    if item_ref is not None and item_ref in policy.forbidden_item_refs:
        raise CommercialError(CommercialCode.DISCOUNT_NOT_ALLOWED)
    if percent > policy.max_percent:
        raise CommercialError(CommercialCode.DISCOUNT_ABOVE_LIMIT)


def set_term(
    uow: UnitOfWork, profile: CommercialProfile, context: OpportunityContext, *, term_type: TermType, term_key: str,
    value: CommercialValue, expected_version: int | None, provenance: ValueSource, correlation_id: str, now: datetime,
) -> CommercialTerm:
    """Create or replace an opportunity-specific approved term (versioned, audited)."""
    validate(profile, term_type, value)
    row_id = term_row_id(context.opportunity.opportunity_id, term_type, term_key)
    current = uow.commercial_terms.get(row_id)
    if (current.version if current else None) != expected_version:
        raise CommercialError(CommercialCode.TERM_VERSION_CHANGED)
    if current is None:
        term = CommercialTerm(term_row_id=row_id, opportunity_id=context.opportunity.opportunity_id, term_type=term_type,
                              term_key=term_key, value=value, provenance=provenance, created_at=now, updated_at=now)
        uow.commercial_terms.add(term)
    else:
        term = current.model_copy(update={"value": value, "provenance": provenance, "updated_at": max(now, current.updated_at),
                                          "version": current.version + 1})
        uow.commercial_terms.update(CommercialTerm.model_validate(term.model_dump()), current.version)
    before: dict[str, JsonValue] = {"value": current.value.display() if current else None}
    record_event(uow, key=(row_id, str(term.version)), event_type="COMMERCIAL_TERM_SET",
                 subjects=(ref(RefKind.COMMERCIAL_TERM, row_id), ref(RefKind.OPPORTUNITY, context.opportunity.opportunity_id)),
                 before=before, after={"term_type": term_type.value, "term_key": term_key, "value": value.display(),
                                       "source": provenance.source.value, "request_id": provenance.request_id,
                                       "version": term.version},
                 actor=operator_actor(provenance.operator_id or "unknown"), correlation_id=correlation_id, now=now)
    return term


def open_request(uow: UnitOfWork, request_id: str, expected_version: int) -> TermRequest:
    request = uow.term_requests.get(request_id)
    if request is None:
        raise CommercialNotFoundError(f"term request {request_id} not found")
    if request.version != expected_version:
        raise CommercialError(CommercialCode.REQUEST_VERSION_CHANGED)
    if request.status not in OPEN_REQUEST_STATUSES:
        raise CommercialError(CommercialCode.REQUEST_NOT_OPEN)
    return request


def approve_request(
    uow: UnitOfWork, profile: CommercialProfile, *, request_id: str, expected_version: int,
    expected_term_version: int | None, operator_id: str, command_id: str, correlation_id: str, now: datetime,
) -> tuple[TermRequest, CommercialTerm]:
    """``expected_term_version`` is the approved term's version the operator saw (None: none
    existed); a term changed meanwhile makes the approval stale, never an overwrite."""
    request = open_request(uow, request_id, expected_version)
    context = load_opportunity(uow, request.opportunity_id)
    require_active(uow, context, now)
    current = uow.commercial_terms.get(term_row_id(request.opportunity_id, request.term_type, request.term_key))
    provenance = ValueSource(source=TermSource.TERM_REQUEST, operator_id=operator_id, command_id=command_id,
                             request_id=request_id, recorded_at=now)
    if (current.version if current else None) != expected_term_version:
        raise CommercialError(CommercialCode.TERM_VERSION_CHANGED)
    term = set_term(uow, profile, context, term_type=request.term_type, term_key=request.term_key,
                    value=request.requested_value, expected_version=expected_term_version,
                    provenance=provenance, correlation_id=correlation_id, now=now)
    approved = _resolve_request(uow, request, TermRequestStatus.APPROVED, operator_id=operator_id, reason=None,
                                correlation_id=correlation_id, now=now)
    for other in uow.term_requests.list_for_opportunity(request.opportunity_id):
        if (other.request_id != request_id and other.status in OPEN_REQUEST_STATUSES
                and (other.term_type, other.term_key) == (request.term_type, request.term_key)):
            _resolve_request(uow, other, TermRequestStatus.SUPERSEDED, operator_id=operator_id,
                             reason="TERM_DECIDED", correlation_id=correlation_id, now=now)
    return approved, term


def reject_request(
    uow: UnitOfWork, *, request_id: str, expected_version: int, reason: str, operator_id: str, correlation_id: str,
    now: datetime,
) -> TermRequest:
    request = open_request(uow, request_id, expected_version)
    return _resolve_request(uow, request, TermRequestStatus.REJECTED, operator_id=operator_id, reason=reason,
                            correlation_id=correlation_id, now=now)


def _resolve_request(uow: UnitOfWork, request: TermRequest, status: TermRequestStatus, *, operator_id: str | None,
                     reason: str | None, correlation_id: str, now: datetime) -> TermRequest:
    resolved = TermRequest.model_validate(request.model_dump() | {
        "status": status, "resolved_by": operator_id, "resolution_reason": reason, "resolved_at": now,
        "updated_at": max(now, request.updated_at), "version": request.version + 1})
    uow.term_requests.update(resolved, request.version)
    record_event(uow, key=(request.request_id, str(resolved.version)), event_type=f"TERM_REQUEST_{status.value}",
                 subjects=(ref(RefKind.TERM_REQUEST, request.request_id), ref(RefKind.OPPORTUNITY, request.opportunity_id)),
                 before={"status": request.status.value},
                 after={"status": status.value, "term_type": request.term_type.value, "reason": reason},
                 actor=operator_actor(operator_id) if operator_id else PIPELINE_ACTOR, correlation_id=correlation_id, now=now)
    return resolved


def cancel_open_requests(uow: UnitOfWork, opportunity_id: str, *, correlation_id: str, now: datetime) -> int:
    count = 0
    for request in uow.term_requests.list_for_opportunity(opportunity_id):
        if request.status in OPEN_REQUEST_STATUSES:
            _resolve_request(uow, request, TermRequestStatus.CANCELLED, operator_id=None, reason="OPPORTUNITY_CLOSED",
                             correlation_id=correlation_id, now=now)
            count += 1
    return count
