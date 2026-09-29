"""Typed results and read models of the follow-up workflow."""

from enum import StrEnum

from pydantic import AwareDatetime

from app.core.enums import ConversationStatus, FollowUpJobStatus, LeadStage, LeadStatus
from app.core.models.base import CoreModel
from app.core.models.types import EntityId, NonEmptyStr, Version


class ScheduleOutcome(StrEnum):
    SCHEDULED = "SCHEDULED"
    ALREADY_SCHEDULED = "ALREADY_SCHEDULED"  # the same logical follow-up is already open
    ALREADY_EXECUTED = "ALREADY_EXECUTED"  # the same logical follow-up already produced its draft
    BLOCKED = "BLOCKED"
    STALE_SNAPSHOT = "STALE_SNAPSHOT"  # the caller's conversation version is no longer current


class ScheduleResult(CoreModel):
    conversation_id: EntityId
    outcome: ScheduleOutcome
    follow_up_id: EntityId | None = None
    due_at: AwareDatetime | None = None
    reason_codes: tuple[NonEmptyStr, ...] = ()
    conversation_version: Version


class FollowUpClaim(CoreModel):
    """A worker's lease on one due job. Only the holder of the current token can execute it."""

    follow_up_id: EntityId
    conversation_id: EntityId
    claim_token: EntityId
    claimed_by: NonEmptyStr
    lease_expires_at: AwareDatetime
    # True when the job was taken over from a worker whose lease expired.
    recovered: bool


class ExecutionOutcome(StrEnum):
    DRAFT_CREATED = "DRAFT_CREATED"  # a reviewable follow-up draft exists; the job is COMPLETED
    REPLAYED = "REPLAYED"  # the job was already completed; nothing new was created
    STALE_CLAIM = "STALE_CLAIM"  # the claim is no longer current (reclaimed, superseded, cancelled)
    SUPERSEDED = "SUPERSEDED"  # newer conversation activity made the follow-up stale
    BLOCKED = "BLOCKED"  # execution-time revalidation refused it
    DEFERRED = "DEFERRED"  # a temporary Stage 3 hold; rescheduled for later


class ExecutionResult(CoreModel):
    follow_up_id: EntityId
    outcome: ExecutionOutcome
    job_status: FollowUpJobStatus
    outbound_id: EntityId | None = None
    due_at: AwareDatetime | None = None
    reason_codes: tuple[NonEmptyStr, ...] = ()


class FollowUpJobView(CoreModel):
    follow_up_id: EntityId
    sequence_no: int
    status: FollowUpJobStatus
    due_at: AwareDatetime
    outbound_id: EntityId | None
    block_codes: tuple[str, ...]


class ConversationView(CoreModel):
    """Operational status next to (never merged with) the lead's pipeline stage."""

    conversation_id: EntityId
    thread_id: EntityId
    lead_id: EntityId
    status: ConversationStatus
    lead_stage: LeadStage | None
    lead_status: LeadStatus | None
    association_certain: bool
    last_inbound_message_id: EntityId | None
    last_inbound_at: AwareDatetime | None
    last_outbound_id: EntityId | None
    last_outbound_at: AwareDatetime | None
    last_activity_at: AwareDatetime
    next_follow_up_at: AwareDatetime | None
    follow_up_count: int
    version: Version
    jobs: tuple[FollowUpJobView, ...]
