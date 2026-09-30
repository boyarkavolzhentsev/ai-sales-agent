"""The structured proposal content contract (not prose, not email).

``build_proposal_draft`` turns one revision into what may be said about it. A claim is
ALLOWED only when it is traceable to an approved durable value (an operator command, an
operator-approved request, a configured default, an approved knowledge fact, or the
totals frozen at approval) and carries that provenance. Everything else is a BLOCKED
claim with a reason: unpriced lines, customer requests awaiting a decision, tax (never
known here), and totals of an unapproved draft that cannot be computed. A later live
model may phrase ``allowed_claims``; it must not add to them.
"""

from datetime import datetime

from app.commercial.config import CommercialProfile
from app.commercial.money import line_total
from app.commercial.state import CommercialFacts, draft_totals, readiness
from app.commercial.terms import effective_terms
from app.core.enums import RevisionStatus, TermType
from app.core.models import AppliedTerm, ProposalRevision, ValueSource
from app.core.models.base import CoreModel
from app.core.models.types import EntityId


class Claim(CoreModel):
    subject: str
    text: str
    provenance: tuple[ValueSource, ...]


class BlockedClaim(CoreModel):
    subject: str
    reason: str


class ProposalDraft(CoreModel):
    revision_id: EntityId
    revision: int
    status: RevisionStatus
    currency: str
    allowed_claims: tuple[Claim, ...]
    blocked_claims: tuple[BlockedClaim, ...]
    terms: tuple[AppliedTerm, ...]
    assumptions: tuple[str, ...]
    exclusions: tuple[str, ...]
    missing: tuple[str, ...]
    unresolved_request_ids: tuple[EntityId, ...]


def build_proposal_draft(profile: CommercialProfile, facts: CommercialFacts, revision: ProposalRevision,
                         now: datetime) -> ProposalDraft:
    frozen = revision.status is not RevisionStatus.DRAFT
    decimals = profile.decimals(revision.currency) if revision.currency in profile.currencies else 2
    allowed: list[Claim] = []
    blocked: list[BlockedClaim] = []
    for line in revision.lines:
        subject = f"LINE:{line.line_id}"
        total = line_total(line, revision.currency, decimals)
        if line.unit_price is None or line.price_source is None or total is None:
            blocked.append(BlockedClaim(subject=subject, reason="PRICE_NOT_APPROVED"))
            continue
        sources = (line.price_source, *((line.discount_source,) if line.discount_source else ()))
        text = f"{line.item_ref}: {line.quantity} {line.unit} at {line.unit_price.amount} {line.unit_price.currency}"
        if line.discount_percent is not None:
            text += f", discount {line.discount_percent}%"
        allowed.append(Claim(subject=subject, text=f"{text} = {total.total.amount} {revision.currency}", provenance=sources))
    terms = revision.frozen_terms if frozen else effective_terms(profile, revision, list(facts.terms), now)
    for term in terms:
        allowed.append(Claim(subject=f"TERM:{term.term_type.value}:{term.term_key}",
                             text=f"{term.term_type.value}: {term.value.display()}", provenance=(term.provenance,)))
    totals = revision.totals if frozen else draft_totals(profile, facts, revision)
    if totals is not None:
        # The total is exactly the arithmetic of approved inputs: its provenance is theirs.
        basis = [s for line in revision.lines for s in (line.price_source, line.discount_source) if s is not None]
        basis += [t.provenance for t in terms if t.term_type is TermType.DISCOUNT]
        allowed.append(Claim(subject="TOTAL", text=f"Total (excluding tax): {totals.total.amount} {totals.currency}",
                             provenance=tuple(basis)))
    else:
        blocked.append(BlockedClaim(subject="TOTAL", reason="TOTAL_UNKNOWN"))
    blocked.append(BlockedClaim(subject="TAX", reason="TAX_NOT_COMPUTED"))
    open_requests = facts.open_requests
    for request in open_requests:
        blocked.append(BlockedClaim(subject=f"REQUEST:{request.term_type.value}", reason="CUSTOMER_REQUEST_NOT_APPROVED"))
    ready = readiness(profile, facts) if not frozen else None
    return ProposalDraft(
        revision_id=revision.revision_id, revision=revision.revision, status=revision.status, currency=revision.currency,
        allowed_claims=tuple(allowed), blocked_claims=tuple(blocked), terms=tuple(terms), assumptions=revision.assumptions,
        exclusions=revision.exclusions, missing=ready.missing if ready else (),
        unresolved_request_ids=tuple(r.request_id for r in open_requests),
    )
