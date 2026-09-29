from enum import StrEnum


class OutboundKind(StrEnum):
    FIRST_TOUCH = "FIRST_TOUCH"
    FOLLOW_UP = "FOLLOW_UP"
    REPLY = "REPLY"


class OutboundStatus(StrEnum):
    DRAFTED = "DRAFTED"
    PENDING_REVIEW = "PENDING_REVIEW"
    HELD = "HELD"
    # A human reviewer approved the exact content. No send permit exists yet; a future
    # send gate must re-run every sending check and issue a permit to reach APPROVED.
    OPERATOR_APPROVED = "OPERATOR_APPROVED"
    APPROVED = "APPROVED"
    SENDING = "SENDING"
    SENT = "SENT"
    FAILED = "FAILED"
    BOUNCED = "BOUNCED"
    CANCELLED = "CANCELLED"
    SKIPPED = "SKIPPED"


class OutboundDecision(StrEnum):
    SEND = "SEND"
    HOLD = "HOLD"
    ESCALATE = "ESCALATE"
    SKIP = "SKIP"


class FollowUpStatus(StrEnum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    EXHAUSTED = "EXHAUSTED"
    CANCELLED = "CANCELLED"


class FollowUpCancelReason(StrEnum):
    REPLY_RECEIVED = "REPLY_RECEIVED"
    SUPPRESSED = "SUPPRESSED"
    BOUNCED = "BOUNCED"
    LEAD_CLOSED = "LEAD_CLOSED"
    OPERATOR = "OPERATOR"
    CAMPAIGN_ENDED = "CAMPAIGN_ENDED"
    OPERATOR_TOOK_OVER = "OPERATOR_TOOK_OVER"
