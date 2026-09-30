"""Commercial decisioning: offers/proposals, terms, negotiation and objections (Stage 13,
logic only; no provider, no live model, no pricing/tax/currency service).

Answers, per opportunity: is it ready for a proposal, which commercial inputs are known
or missing, which terms are approved and which only requested, what may be said (every
claim traceable to an approved durable value), what needs an operator, which objections
and negotiation requests are open, and what the next commercial action is.

- ``proposals``: one proposal per opportunity as revisions; approval freezes terms and
  Decimal totals; presentation, acceptance and decline are operator confirmations.
- ``terms``: approved opportunity-specific terms, the precedence rules, and customer
  term requests (a request is never an approved value).
- ``negotiation``: requests, objections and acceptance/decline signals from customer
  messages; operator handling of objections and signals.
- ``state`` / ``views`` / ``draft``: readiness, next action, blockers, read models,
  queues, metrics and the structured proposal content contract.
- ``pricing``: prices only from operators or approved internal knowledge facts.
- ``contracts`` / ``fake``: the provider-neutral extraction contract and test fakes.

Nothing here marks a lead WON or LOST, and no AI output becomes a commercial value.
"""

from app.commercial.config import GENERIC_COMMERCIAL_PROFILE, CommercialConfig, CommercialProfile, DiscountPolicy
from app.commercial.contracts import (
    CommercialExtraction,
    CommercialExtractionRequest,
    CommercialExtractor,
    ObjectionProposal,
    RequestedTerm,
)
from app.commercial.draft import BlockedClaim, Claim, ProposalDraft
from app.commercial.errors import CommercialCode, CommercialError, CommercialNotFoundError
from app.commercial.pricing import KnowledgePriceCatalog, PriceCatalog, PriceFact
from app.commercial.proposals import LineInput, TermInput
from app.commercial.service import CommercialHookOutcome, CommercialHookStatus, CommercialService
from app.commercial.state import CommercialNextAction, ProposalReadiness, ReadinessLevel
from app.commercial.views import CommercialMetrics, CommercialQueue, CommercialView, CurrencyAmount

__all__ = [
    "GENERIC_COMMERCIAL_PROFILE",
    "BlockedClaim",
    "Claim",
    "CommercialCode",
    "CommercialConfig",
    "CommercialError",
    "CommercialExtraction",
    "CommercialExtractionRequest",
    "CommercialExtractor",
    "CommercialHookOutcome",
    "CommercialHookStatus",
    "CommercialMetrics",
    "CommercialNextAction",
    "CommercialNotFoundError",
    "CommercialProfile",
    "CommercialQueue",
    "CommercialService",
    "CommercialView",
    "CurrencyAmount",
    "DiscountPolicy",
    "KnowledgePriceCatalog",
    "LineInput",
    "ObjectionProposal",
    "PriceCatalog",
    "PriceFact",
    "ProposalDraft",
    "ProposalReadiness",
    "ReadinessLevel",
    "RequestedTerm",
    "TermInput",
]
