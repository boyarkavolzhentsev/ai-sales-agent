"""Commercial decisioning (Stage 13) enums: proposals, terms, requests, objections, signals."""

from enum import StrEnum


class RevisionStatus(StrEnum):
    """One proposal revision. Only DRAFT content is editable; APPROVED and later revisions
    are frozen (a change needs a new revision)."""

    DRAFT = "DRAFT"  # being prepared by an operator
    APPROVED = "APPROVED"  # an operator approved it; terms and totals frozen
    PRESENTED = "PRESENTED"  # an operator confirmed it was actually communicated
    ACCEPTED = "ACCEPTED"  # an operator confirmed the customer's commercial acceptance
    DECLINED = "DECLINED"  # an operator confirmed the customer declined it
    WITHDRAWN = "WITHDRAWN"  # an operator withdrew it
    SUPERSEDED = "SUPERSEDED"  # replaced by the next revision (content unchanged)
    CLOSED = "CLOSED"  # the lead/opportunity closed while it was still open


class CommercialStage(StrEnum):
    """Derived (never stored) commercial position of an opportunity."""

    NOT_STARTED = "NOT_STARTED"
    PREPARING = "PREPARING"
    READY_FOR_REVIEW = "READY_FOR_REVIEW"
    APPROVED = "APPROVED"
    PRESENTED = "PRESENTED"
    NEGOTIATING = "NEGOTIATING"
    ACCEPTED = "ACCEPTED"
    DECLINED = "DECLINED"
    WITHDRAWN = "WITHDRAWN"
    CLOSED = "CLOSED"


class TermType(StrEnum):
    PRICE = "PRICE"
    DISCOUNT = "DISCOUNT"
    PAYMENT_TERM = "PAYMENT_TERM"
    BILLING_CADENCE = "BILLING_CADENCE"
    CONTRACT_LENGTH = "CONTRACT_LENGTH"
    DELIVERY_WINDOW = "DELIVERY_WINDOW"
    IMPLEMENTATION_SCOPE = "IMPLEMENTATION_SCOPE"
    SLA = "SLA"
    WARRANTY = "WARRANTY"
    TAX = "TAX"
    CURRENCY = "CURRENCY"
    VALIDITY_PERIOD = "VALIDITY_PERIOD"
    LEGAL_TERM = "LEGAL_TERM"  # a reference to an approved legal term, never generated text
    CUSTOM_TERM = "CUSTOM_TERM"


class ValueKind(StrEnum):
    TEXT = "TEXT"
    MONEY = "MONEY"
    PERCENT = "PERCENT"


class TermSource(StrEnum):
    """Where an approved value came from. A customer request is never a source."""

    OPERATOR = "OPERATOR"  # set by an operator command
    TERM_REQUEST = "TERM_REQUEST"  # an operator approved a customer's request
    PROFILE_DEFAULT = "PROFILE_DEFAULT"  # explicitly configured in the commercial profile
    REVISION_OVERRIDE = "REVISION_OVERRIDE"  # set by an operator on one proposal revision
    KNOWLEDGE = "KNOWLEDGE"  # an approved, current internal knowledge fact (prices)


class TermRequestStatus(StrEnum):
    REQUESTED = "REQUESTED"  # nothing approved exists for this term yet
    UNDER_REVIEW = "UNDER_REVIEW"  # conflicts with an approved value; an operator decides
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    SUPERSEDED = "SUPERSEDED"
    CANCELLED = "CANCELLED"  # the opportunity/lead closed


class ObjectionCategory(StrEnum):
    PRICE = "PRICE"
    BUDGET = "BUDGET"
    TIMING = "TIMING"
    COMPETITOR = "COMPETITOR"
    PRODUCT_FIT = "PRODUCT_FIT"
    FEATURE_GAP = "FEATURE_GAP"
    TRUST = "TRUST"
    PROCUREMENT = "PROCUREMENT"
    LEGAL = "LEGAL"
    SECURITY = "SECURITY"
    IMPLEMENTATION = "IMPLEMENTATION"
    PAYMENT_TERMS = "PAYMENT_TERMS"
    OTHER = "OTHER"


class ObjectionStatus(StrEnum):
    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RESOLVED = "RESOLVED"
    WITHDRAWN = "WITHDRAWN"


class SignalKind(StrEnum):
    ACCEPTANCE = "ACCEPTANCE"  # the customer's words suggest acceptance: not a decision
    DECLINE = "DECLINE"  # the customer's words suggest a decline: not a decision


class SignalStatus(StrEnum):
    OPEN = "OPEN"  # awaiting an operator
    CONFIRMED = "CONFIRMED"  # the operator recorded the matching proposal decision
    DISMISSED = "DISMISSED"  # the operator judged it not a decision
    SUPERSEDED = "SUPERSEDED"  # a newer customer message produced a newer signal
    CANCELLED = "CANCELLED"  # the opportunity/lead closed


class CommercialAction(StrEnum):
    COMPLETE_COMMERCIAL_INPUTS = "COMPLETE_COMMERCIAL_INPUTS"
    REVIEW_TERM_REQUEST = "REVIEW_TERM_REQUEST"
    PREPARE_PROPOSAL = "PREPARE_PROPOSAL"
    REVIEW_PROPOSAL = "REVIEW_PROPOSAL"
    PRESENT_PROPOSAL = "PRESENT_PROPOSAL"
    WAIT_FOR_CUSTOMER_DECISION = "WAIT_FOR_CUSTOMER_DECISION"
    HANDLE_OBJECTION = "HANDLE_OBJECTION"
    REVIEW_REVISION = "REVIEW_REVISION"
    CONFIRM_ACCEPTANCE = "CONFIRM_ACCEPTANCE"
    COMPLETE_WON = "COMPLETE_WON"
    DECIDE_LOSS = "DECIDE_LOSS"
    CLOSED = "CLOSED"
    NONE = "NONE"


class CommercialBlocker(StrEnum):
    DNC = "DNC"
    LEAD_CLOSED = "LEAD_CLOSED"
    OPPORTUNITY_NOT_OPEN = "OPPORTUNITY_NOT_OPEN"
    QUALIFICATION_NOT_APPROVED = "QUALIFICATION_NOT_APPROVED"
    QUALIFICATION_CONFLICT = "QUALIFICATION_CONFLICT"
    NO_PROPOSAL = "NO_PROPOSAL"
    NO_PROPOSAL_LINES = "NO_PROPOSAL_LINES"
    MISSING_PRICE = "MISSING_PRICE"
    MISSING_CURRENCY = "MISSING_CURRENCY"
    CURRENCY_NOT_ALLOWED = "CURRENCY_NOT_ALLOWED"
    MISSING_REQUIRED_TERM = "MISSING_REQUIRED_TERM"
    DISCOUNT_NOT_ALLOWED = "DISCOUNT_NOT_ALLOWED"
    UNAPPROVED_TERM_REQUEST = "UNAPPROVED_TERM_REQUEST"
    OPEN_OBJECTION = "OPEN_OBJECTION"
    PROPOSAL_NOT_APPROVED = "PROPOSAL_NOT_APPROVED"
    PROPOSAL_NOT_PRESENTED = "PROPOSAL_NOT_PRESENTED"
    PROPOSAL_REVISION_REQUIRED = "PROPOSAL_REVISION_REQUIRED"
    ACCEPTANCE_SIGNAL = "ACCEPTANCE_SIGNAL"
    DECLINE_SIGNAL = "DECLINE_SIGNAL"
