"""Campaign execution configuration, typed results and read models."""

from datetime import timedelta
from enum import StrEnum
from typing import Annotated

from pydantic import AwareDatetime, Field

from app.core.enums import CampaignJobStatus, CampaignMemberStatus, CampaignStatus
from app.core.models.base import CoreModel
from app.core.models.types import EntityId, NonEmptyStr, Version
from app.llm import SenderIdentity
from app.policy import KillSwitchState, LimitPolicy, SendingWindow


class CampaignExecutionConfig(CoreModel):
    sender: SenderIdentity
    limits: LimitPolicy
    window: SendingWindow
    kill_switch: KillSwitchState
    # The knowledge question whose approved answer may be quoted as the value statement of
    # a first touch; per campaign override by campaign_id. Unsupported: generic wording.
    value_question: NonEmptyStr = "What does the product integrate with?"
    value_questions: dict[str, NonEmptyStr] = {}
    lease: Annotated[timedelta, Field(gt=timedelta(0), le=timedelta(hours=1))] = timedelta(minutes=5)
    defer_delay: Annotated[timedelta, Field(gt=timedelta(0))] = timedelta(hours=1)


class EnrollmentOutcome(StrEnum):
    ENROLLED = "ENROLLED"
    ALREADY_ENROLLED = "ALREADY_ENROLLED"  # idempotent repeat: nothing changed
    ENROLLED_INELIGIBLE = "ENROLLED_INELIGIBLE"  # recorded as SKIPPED/SUPPRESSED, never contacted
    REJECTED = "REJECTED"  # nothing recorded (unknown campaign/contact, or the campaign ended)


class EnrollmentResult(CoreModel):
    outcome: EnrollmentOutcome
    member_id: EntityId | None = None
    status: CampaignMemberStatus | None = None
    reason_codes: tuple[NonEmptyStr, ...] = ()


class ScheduleSummary(CoreModel):
    campaign_id: EntityId
    scheduled: tuple[EntityId, ...] = ()
    exhausted: tuple[EntityId, ...] = ()
    blocked_reason: NonEmptyStr | None = None


class CampaignClaim(CoreModel):
    job_id: EntityId
    member_id: EntityId
    claim_token: EntityId
    claimed_by: NonEmptyStr
    lease_expires_at: AwareDatetime
    recovered: bool


class ExecutionOutcome(StrEnum):
    DRAFT_CREATED = "DRAFT_CREATED"
    REPLAYED = "REPLAYED"
    STALE_CLAIM = "STALE_CLAIM"
    SUPERSEDED = "SUPERSEDED"
    BLOCKED = "BLOCKED"
    DEFERRED = "DEFERRED"
    CANCELLED = "CANCELLED"  # the campaign is no longer executable (paused, ended)


class ExecutionResult(CoreModel):
    job_id: EntityId
    outcome: ExecutionOutcome
    job_status: CampaignJobStatus
    member_status: CampaignMemberStatus | None = None
    outbound_id: EntityId | None = None
    due_at: AwareDatetime | None = None
    reason_codes: tuple[NonEmptyStr, ...] = ()


class MemberView(CoreModel):
    member_id: EntityId
    contact_id: EntityId
    lead_id: EntityId | None
    status: CampaignMemberStatus
    touch_count: int
    latest_outbound_id: EntityId | None
    next_action_at: AwareDatetime | None
    terminal_reason: str | None
    # The latest logical touch (job) and its state.
    latest_touch_no: int | None
    latest_job_status: CampaignJobStatus | None
    version: Version


class CampaignStats(CoreModel):
    """Derived from durable membership state on every read (no separate counters)."""

    campaign_id: EntityId
    campaign_status: CampaignStatus
    total_enrolled: int
    by_status: dict[str, int]
    pending: int  # ENROLLED (not yet drafted)
    awaiting_review: int  # DRAFTED
    approved: int  # APPROVED
    dispatching: int  # DISPATCHING
    waiting: int  # WAITING
    replied: int
    converted: int
    completed: int
    suppressed_or_skipped: int
    failed_or_cancelled: int
    touches_accepted: int
    open_jobs: int
