"""Persistence-infrastructure records that have no core-domain counterpart."""

from datetime import date
from enum import StrEnum
from typing import Self

from pydantic import AwareDatetime, PositiveInt, model_validator

from app.core.enums import OutboundKind, OutboundStatus
from app.core.models.base import CoreModel
from app.core.models.permit import SendPermit
from app.core.models.types import EmailAddress, EntityId, NonEmptyStr, Sha256Hex, Version
from app.core.validation import ensure_not_before


class IdempotencyRecord(CoreModel):
    """A reserved idempotency key and the operation that reserved it."""

    key: NonEmptyStr
    operation: NonEmptyStr
    created_at: AwareDatetime


class LedgerEntry(CoreModel):
    """Read-only projection of one dispatched outbound message, used for quota counting.

    ``mailbox`` is resolved from persisted facts: the campaign's sending mailbox for
    campaign messages, otherwise the thread's mailbox (replies).
    """

    outbound_id: EntityId
    kind: OutboundKind
    status: OutboundStatus
    contact_id: EntityId
    campaign_id: EntityId | None = None
    mailbox: EmailAddress
    sending_at: AwareDatetime


class QuotaReservationState(StrEnum):
    ACTIVE = "ACTIVE"  # slot held; not yet visible in the send ledger
    CONSUMED = "CONSUMED"  # message dispatched; the ledger now counts it
    RELEASED = "RELEASED"  # slot given back without sending


class QuotaReservation(CoreModel):
    """A quota slot held for one outbound message on one local policy date.

    At most one ACTIVE or CONSUMED reservation may exist per outbound message.
    """

    reservation_id: EntityId
    outbound_id: EntityId
    kind: OutboundKind
    policy_date: date
    timezone: NonEmptyStr
    mailbox: EmailAddress
    campaign_id: EntityId | None = None
    contact_id: EntityId
    state: QuotaReservationState = QuotaReservationState.ACTIVE
    created_at: AwareDatetime
    updated_at: AwareDatetime
    # Optimistic-concurrency version; incremented by exactly 1 on every persisted update.
    version: Version = 1

    @model_validator(mode="after")
    def _check_timestamps(self) -> Self:
        ensure_not_before(self.updated_at, self.created_at, "updated_at", "created_at")
        return self


class KnowledgeFactRecord(CoreModel):
    """One structured fact from a fact document, tied to the chunk that states it."""

    source_id: EntityId
    source_version: Version
    fact_key: NonEmptyStr
    value: NonEmptyStr
    unit: NonEmptyStr | None = None
    chunk_id: EntityId


class DispatchAttemptState(StrEnum):
    CLAIMED = "CLAIMED"  # claim committed; the transport outcome is not recorded (yet)
    ACCEPTED = "ACCEPTED"  # the provider confirmed acceptance (not delivery)
    NOT_ACCEPTED = "NOT_ACCEPTED"  # confirmed not accepted (rejected or failed before submission)
    UNKNOWN = "UNKNOWN"  # submission may have happened; needs reconciliation, never resending


UNRESOLVED_ATTEMPT_STATES = frozenset({DispatchAttemptState.CLAIMED, DispatchAttemptState.UNKNOWN})


class DispatchAttempt(CoreModel):
    """One claimed attempt to hand one outbound message to the email transport.

    Carries the single-use SendPermit that authorized it (issued and consumed when the
    claim committed), the quota reservation that accounts for it, and the exact
    recipient, sender mailbox, content hash and Message-ID that were submitted. History
    is never rewritten: each retry is a new attempt with the next ``attempt_no``.
    """

    attempt_id: EntityId
    outbound_id: EntityId
    attempt_no: PositiveInt
    permit: SendPermit
    reservation_id: EntityId
    recipient: EmailAddress
    sender_mailbox: EmailAddress
    content_hash: Sha256Hex
    rfc_message_id: NonEmptyStr
    correlation_id: EntityId
    state: DispatchAttemptState = DispatchAttemptState.CLAIMED
    reason_code: NonEmptyStr | None = None
    retryable: bool = False
    provider_message_id: NonEmptyStr | None = None
    claimed_at: AwareDatetime
    resolved_at: AwareDatetime | None = None
    # Positive acceptance evidence that arrived after this attempt was recorded
    # NOT_ACCEPTED while another attempt of the message was accepted or unresolved. The
    # recorded history stays as it was; this durable evidence blocks every further attempt
    # of the message. It proves this request was accepted, not how many emails arrived.
    late_acceptance_provider_message_id: NonEmptyStr | None = None
    late_acceptance_at: AwareDatetime | None = None
    version: Version = 1

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        if self.permit.outbound_id != self.outbound_id or self.permit.content_hash != self.content_hash:
            raise ValueError("the permit must authorize exactly this message and content")
        if self.permit.consumed_at is None:
            raise ValueError("an attempt exists only once its permit was consumed")
        resolved = self.state in (DispatchAttemptState.ACCEPTED, DispatchAttemptState.NOT_ACCEPTED)
        if resolved != (self.resolved_at is not None):
            raise ValueError("resolved_at is required for, and only allowed on, ACCEPTED/NOT_ACCEPTED")
        if (self.state is DispatchAttemptState.ACCEPTED) != (self.provider_message_id is not None):
            raise ValueError("provider_message_id is required for, and only allowed on, ACCEPTED")
        if self.state in (DispatchAttemptState.NOT_ACCEPTED, DispatchAttemptState.UNKNOWN) and self.reason_code is None:
            raise ValueError(f"{self.state} requires reason_code")
        if self.retryable and self.state is not DispatchAttemptState.NOT_ACCEPTED:
            raise ValueError("only a confirmed non-acceptance can be retryable")
        if (self.late_acceptance_provider_message_id is None) != (self.late_acceptance_at is None):
            raise ValueError("late acceptance evidence needs both provider_message_id and time")
        if self.late_acceptance_at is not None and self.state is not DispatchAttemptState.NOT_ACCEPTED:
            raise ValueError("late acceptance evidence is only recorded on a NOT_ACCEPTED attempt")
        ensure_not_before(self.late_acceptance_at, self.claimed_at, "late_acceptance_at", "claimed_at")
        ensure_not_before(self.resolved_at, self.claimed_at, "resolved_at", "claimed_at")
        return self


class MailboxSyncStatus(StrEnum):
    ACTIVE = "ACTIVE"
    # The provider no longer serves changes since the cursor (e.g. Gmail history expired):
    # only an explicit recovery establishes a new cursor; nothing is replayed silently.
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"


class MailboxSyncState(CoreModel):
    """The inbound synchronization cursor of one provider mailbox. ``cursor`` is an
    opaque provider position (a Gmail historyId); changes after it are not yet handled.
    Infrastructure only: no token, credential or message content."""

    state_id: EntityId
    provider: NonEmptyStr
    mailbox: EmailAddress
    cursor: NonEmptyStr
    status: MailboxSyncStatus = MailboxSyncStatus.ACTIVE
    # Bumped by every explicit (re)initialization of the cursor.
    generation: PositiveInt = 1
    last_synced_at: AwareDatetime | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime
    version: Version = 1


class MailboxSyncFailureStatus(StrEnum):
    OPEN = "OPEN"
    RESOLVED = "RESOLVED"


class MailboxSyncFailure(CoreModel):
    """A provider message whose processing failed. Recorded before the cursor moves past
    it, retried by later syncs, never deleted: one bad message neither blocks nor loses
    later mail. Stores the provider message id and an error code only."""

    failure_id: EntityId
    provider: NonEmptyStr
    mailbox: EmailAddress
    provider_message_id: NonEmptyStr
    status: MailboxSyncFailureStatus = MailboxSyncFailureStatus.OPEN
    attempts: PositiveInt = 1
    last_error_code: NonEmptyStr
    first_failed_at: AwareDatetime
    last_failed_at: AwareDatetime
    resolved_at: AwareDatetime | None = None
    version: Version = 1

    @model_validator(mode="after")
    def _check(self) -> Self:
        if (self.status is MailboxSyncFailureStatus.RESOLVED) != (self.resolved_at is not None):
            raise ValueError("resolved_at is required for, and only allowed on, RESOLVED")
        ensure_not_before(self.last_failed_at, self.first_failed_at, "last_failed_at", "first_failed_at")
        return self
