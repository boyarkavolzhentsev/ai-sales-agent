from typing import Self

from pydantic import AwareDatetime, NonNegativeInt, PositiveInt, model_validator

from app.core.enums import ConversationStatus, FollowUpJobStatus
from app.core.models.base import CoreModel
from app.core.models.types import EntityId, NonEmptyStr, UniqueNonEmptyStrs, Version
from app.core.validation import ensure_not_before

TERMINAL_CONVERSATION_STATUSES = frozenset(
    {ConversationStatus.CONVERTED, ConversationStatus.CLOSED, ConversationStatus.DO_NOT_CONTACT}
)
OPEN_FOLLOW_UP_JOB_STATUSES = frozenset({FollowUpJobStatus.SCHEDULED, FollowUpJobStatus.CLAIMED})


class Conversation(CoreModel):
    """Operational state of one email thread with one lead.

    One conversation per thread (the thread is the deterministic association built by
    Stage 6 from Message-IDs and participants). The lead's pipeline stage stays on the
    Lead and is never copied here. ``association_certain`` is False when the thread was
    created for a message whose thread or lead could not be attributed unambiguously;
    such a conversation is never followed up automatically.
    """

    conversation_id: EntityId
    thread_id: EntityId
    lead_id: EntityId
    contact_id: EntityId
    status: ConversationStatus
    association_certain: bool = True
    last_inbound_message_id: EntityId | None = None
    last_inbound_at: AwareDatetime | None = None
    last_outbound_id: EntityId | None = None
    last_outbound_at: AwareDatetime | None = None
    last_activity_at: AwareDatetime
    next_follow_up_at: AwareDatetime | None = None
    # Follow-ups the provider accepted in this conversation.
    follow_up_count: NonNegativeInt = 0
    created_at: AwareDatetime
    updated_at: AwareDatetime
    version: Version = 1

    @model_validator(mode="after")
    def _check(self) -> Self:
        if (self.last_inbound_message_id is None) != (self.last_inbound_at is None):
            raise ValueError("last_inbound_message_id and last_inbound_at are set together")
        if (self.last_outbound_id is None) != (self.last_outbound_at is None):
            raise ValueError("last_outbound_id and last_outbound_at are set together")
        if (self.next_follow_up_at is not None) != (self.status is ConversationStatus.FOLLOW_UP_DUE):
            raise ValueError("next_follow_up_at is required for, and only allowed on, FOLLOW_UP_DUE")
        ensure_not_before(self.updated_at, self.created_at, "updated_at", "created_at")
        return self


class FollowUpJob(CoreModel):
    """One logical follow-up: "follow-up number ``sequence_no`` after outbound message
    ``anchor_outbound_id``". Its identity is derived from exactly that, so the same logical
    follow-up can be scheduled only once, whatever retries or restarts happen.

    ``basis_conversation_version`` is the conversation version the job was scheduled
    (or last deferred) against; any later conversation change makes it stale.
    """

    follow_up_id: EntityId
    conversation_id: EntityId
    anchor_outbound_id: EntityId
    sequence_no: PositiveInt
    basis_conversation_version: Version
    due_at: AwareDatetime
    status: FollowUpJobStatus = FollowUpJobStatus.SCHEDULED
    reason: NonEmptyStr = "NO_REPLY"
    block_codes: UniqueNonEmptyStrs = ()
    claim_token: EntityId | None = None
    claimed_by: NonEmptyStr | None = None
    lease_expires_at: AwareDatetime | None = None
    claim_count: NonNegativeInt = 0
    # The follow-up draft (a reviewable REPLY outbound message) produced by execution.
    outbound_id: EntityId | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime
    version: Version = 1

    @model_validator(mode="after")
    def _check(self) -> Self:
        claimed = self.status is FollowUpJobStatus.CLAIMED
        if claimed != (self.claim_token is not None) or claimed != (self.lease_expires_at is not None):
            raise ValueError("claim_token and lease_expires_at are required for, and only allowed on, CLAIMED")
        if claimed and self.claimed_by is None:
            raise ValueError("a CLAIMED job requires claimed_by")
        if (self.status is FollowUpJobStatus.COMPLETED) != (self.outbound_id is not None):
            raise ValueError("outbound_id is required for, and only allowed on, COMPLETED")
        if (self.status is FollowUpJobStatus.BLOCKED) != bool(self.block_codes):
            raise ValueError("block_codes are required for, and only allowed on, BLOCKED")
        ensure_not_before(self.updated_at, self.created_at, "updated_at", "created_at")
        return self
