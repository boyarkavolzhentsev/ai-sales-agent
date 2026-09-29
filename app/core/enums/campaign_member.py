from enum import StrEnum


class CampaignMemberStatus(StrEnum):
    """One contact's position in one campaign's outreach sequence (before any reply).

    Distinct from LeadStage (pipeline), LeadStatus (who drives the lead),
    ConversationStatus (a two-way conversation), OutboundStatus (one message) and
    DispatchAttemptState (one hand-off to the transport)."""

    ENROLLED = "ENROLLED"  # enrolled; its next touch is not drafted yet
    DRAFTED = "DRAFTED"  # the current touch is a draft awaiting operator review
    APPROVED = "APPROVED"  # the operator approved it; awaiting dispatch (or a permitted retry)
    DISPATCHING = "DISPATCHING"  # handed to the transport; outcome not confirmed yet
    WAITING = "WAITING"  # the touch was accepted by the provider; awaiting a reply or the next touch
    # Terminal for campaign automation:
    REPLIED = "REPLIED"  # the contact wrote: control passed to the inbound conversation workflow
    CONVERTED = "CONVERTED"  # the lead was won
    COMPLETED = "COMPLETED"  # the sequence ended without a reply
    SKIPPED = "SKIPPED"  # not eligible (e.g. an active lead or conversation already exists)
    SUPPRESSED = "SUPPRESSED"  # do-not-contact
    FAILED = "FAILED"  # a touch could not be delivered to the provider and may not be retried
    CANCELLED = "CANCELLED"  # stopped by an operator or by the campaign ending


class CampaignJobStatus(StrEnum):
    """One logical campaign touch's execution state (not a worker attempt)."""

    SCHEDULED = "SCHEDULED"
    CLAIMED = "CLAIMED"
    COMPLETED = "COMPLETED"  # its draft exists
    CANCELLED = "CANCELLED"
    BLOCKED = "BLOCKED"
    SUPERSEDED = "SUPERSEDED"
