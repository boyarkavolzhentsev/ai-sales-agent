from enum import StrEnum


class ConversationStatus(StrEnum):
    """Operational state of one conversation (thread). Orthogonal to the lead's pipeline
    stage (LeadStage) and to who drives the lead (LeadStatus)."""

    ACTIVE = "ACTIVE"  # the customer wrote last; a response (draft or escalation) is pending
    WAITING_FOR_REPLY = "WAITING_FOR_REPLY"  # our last message was accepted; awaiting the customer
    FOLLOW_UP_DUE = "FOLLOW_UP_DUE"  # a follow-up job is scheduled or being executed
    OPERATOR_REVIEW = "OPERATOR_REVIEW"  # a human must act (escalation or a follow-up draft to review)
    PAUSED = "PAUSED"  # an operator paused automation for this conversation
    CONVERTED = "CONVERTED"  # the lead was won
    CLOSED = "CLOSED"  # closed by the lead outcome or by an operator
    DO_NOT_CONTACT = "DO_NOT_CONTACT"  # the contact is suppressed


class FollowUpJobStatus(StrEnum):
    SCHEDULED = "SCHEDULED"  # waiting for due_at
    CLAIMED = "CLAIMED"  # leased by a worker; an expired lease is claimable again
    COMPLETED = "COMPLETED"  # its follow-up draft exists (review and dispatch happen downstream)
    CANCELLED = "CANCELLED"  # stopped by an operator or by suppression/closure
    BLOCKED = "BLOCKED"  # execution-time revalidation refused it permanently
    SUPERSEDED = "SUPERSEDED"  # newer conversation activity made it stale
