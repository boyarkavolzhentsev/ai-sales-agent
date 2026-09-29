from typing import Self

from pydantic import AwareDatetime, NonNegativeInt, PositiveInt, model_validator

from app.core.enums import CampaignJobStatus, CampaignMemberStatus
from app.core.models.base import CoreModel
from app.core.models.types import EntityId, NonEmptyStr, UniqueNonEmptyStrs, Version
from app.core.validation import ensure_not_before

TERMINAL_MEMBER_STATUSES = frozenset(
    {
        CampaignMemberStatus.REPLIED,
        CampaignMemberStatus.CONVERTED,
        CampaignMemberStatus.COMPLETED,
        CampaignMemberStatus.SKIPPED,
        CampaignMemberStatus.SUPPRESSED,
        CampaignMemberStatus.FAILED,
        CampaignMemberStatus.CANCELLED,
    }
)
OPEN_CAMPAIGN_JOB_STATUSES = frozenset({CampaignJobStatus.SCHEDULED, CampaignJobStatus.CLAIMED})


class CampaignMember(CoreModel):
    """One contact enrolled in one campaign: one logical membership, whose identity is
    derived from (campaign, contact). The Lead and ProspectContact stay the canonical
    business entities; this records only the campaign sequence position.

    ``thread_id`` is the single email thread all of this membership's touches use; it
    exists from the first draft on, so a reply to any touch joins it.
    """

    member_id: EntityId
    campaign_id: EntityId
    contact_id: EntityId
    lead_id: EntityId | None = None
    thread_id: EntityId | None = None
    status: CampaignMemberStatus = CampaignMemberStatus.ENROLLED
    enrolled_at: AwareDatetime
    latest_outbound_id: EntityId | None = None
    # Touches the provider accepted (first touch included).
    touch_count: NonNegativeInt = 0
    last_activity_at: AwareDatetime
    next_action_at: AwareDatetime | None = None
    terminal_reason: NonEmptyStr | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime
    version: Version = 1

    @model_validator(mode="after")
    def _check(self) -> Self:
        terminal = self.status in TERMINAL_MEMBER_STATUSES
        if terminal != (self.terminal_reason is not None):
            raise ValueError("terminal_reason is required for, and only allowed on, a terminal status")
        if terminal and self.next_action_at is not None:
            raise ValueError("a terminal membership has no next action")
        if self.status is not CampaignMemberStatus.SKIPPED and self.status is not CampaignMemberStatus.SUPPRESSED and self.lead_id is None:
            raise ValueError("an eligible membership requires lead_id")
        ensure_not_before(self.updated_at, self.created_at, "updated_at", "created_at")
        return self


class CampaignJob(CoreModel):
    """One logical campaign touch: "touch N of membership M". Its identity is derived from
    exactly that, so a touch can be scheduled once and can produce at most one message.
    ``touch_no`` 1 is the first touch; N > 1 is follow-up N - 1 of the FollowUpPlan."""

    job_id: EntityId
    member_id: EntityId
    campaign_id: EntityId
    touch_no: PositiveInt
    basis_member_version: Version
    due_at: AwareDatetime
    status: CampaignJobStatus = CampaignJobStatus.SCHEDULED
    block_codes: UniqueNonEmptyStrs = ()
    reason: NonEmptyStr | None = None
    claim_token: EntityId | None = None
    claimed_by: NonEmptyStr | None = None
    lease_expires_at: AwareDatetime | None = None
    claim_count: NonNegativeInt = 0
    outbound_id: EntityId | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime
    version: Version = 1

    @model_validator(mode="after")
    def _check(self) -> Self:
        claimed = self.status is CampaignJobStatus.CLAIMED
        if claimed != (self.claim_token is not None) or claimed != (self.lease_expires_at is not None):
            raise ValueError("claim_token and lease_expires_at are required for, and only allowed on, CLAIMED")
        if claimed and self.claimed_by is None:
            raise ValueError("a CLAIMED job requires claimed_by")
        if (self.status is CampaignJobStatus.COMPLETED) != (self.outbound_id is not None):
            raise ValueError("outbound_id is required for, and only allowed on, COMPLETED")
        if (self.status is CampaignJobStatus.BLOCKED) != bool(self.block_codes):
            raise ValueError("block_codes are required for, and only allowed on, BLOCKED")
        ensure_not_before(self.updated_at, self.created_at, "updated_at", "created_at")
        return self
