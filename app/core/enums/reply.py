from enum import StrEnum


class ReplyDecision(StrEnum):
    """Inbound reply decision.

    AUTO_REPLY exists as a contract value but is disabled in the initial V1 mode.
    """

    AUTO_REPLY = "AUTO_REPLY"
    DRAFT_FOR_REVIEW = "DRAFT_FOR_REVIEW"
    ESCALATE = "ESCALATE"
    NO_ACTION = "NO_ACTION"


class ConfidenceBand(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class RiskFlag(StrEnum):
    LEGAL = "LEGAL"
    COMPLAINT = "COMPLAINT"
    NEGOTIATION = "NEGOTIATION"
    INJECTION_SUSPECTED = "INJECTION_SUSPECTED"
    SENSITIVE = "SENSITIVE"


class DraftPurpose(StrEnum):
    INBOUND_REPLY = "INBOUND_REPLY"
    OUTBOUND_FIRST_TOUCH = "OUTBOUND_FIRST_TOUCH"
    OUTBOUND_FOLLOW_UP = "OUTBOUND_FOLLOW_UP"


class ClaimCheckStatus(StrEnum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


class DraftReviewStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
