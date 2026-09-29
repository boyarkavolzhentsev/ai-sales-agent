"""Dispatch configuration, request and typed result."""

from datetime import timedelta
from enum import StrEnum
from typing import Annotated

from pydantic import AwareDatetime, Field

from app.core.enums import OutboundStatus
from app.core.models.base import CoreModel
from app.core.models.types import EmailAddress, EntityId, NonEmptyStr, UniqueEmailAddresses
from app.llm import SenderIdentity
from app.policy import KillSwitchState, LimitPolicy, SendingWindow


class DispatchConfig(CoreModel):
    """Application-owned sending identity and the Stage 3 policy inputs."""

    # Mailboxes this application may send from; a reply goes from its thread's mailbox.
    sender_mailboxes: Annotated[UniqueEmailAddresses, Field(min_length=1)]
    sender: SenderIdentity
    limits: LimitPolicy
    window: SendingWindow
    kill_switch: KillSwitchState
    permit_ttl: Annotated[timedelta, Field(gt=timedelta(0), le=timedelta(minutes=30))] = timedelta(minutes=5)
    # Total attempts per message, including the first. Retries are never scheduled here.
    max_attempts: Annotated[int, Field(ge=1, le=5)] = 3


class DispatchRequest(CoreModel):
    """Trusted internal entry point: a reference only. Recipients, content, permits and
    policy decisions always come from persistence, never from the caller."""

    outbound_id: EntityId
    correlation_id: EntityId


class DispatchOutcome(StrEnum):
    ACCEPTED = "ACCEPTED"  # provider acceptance confirmed (not delivery)
    NOT_ACCEPTED = "NOT_ACCEPTED"  # confirmed not accepted
    UNKNOWN = "UNKNOWN"  # an attempt is unresolved; reconcile, never resend
    BLOCKED = "BLOCKED"  # no attempt was claimed; nothing was submitted


class DispatchCode(StrEnum):
    """Dispatch-specific reason codes. Results also carry Stage 7 gate codes and Stage 3
    policy reasons verbatim."""

    NOT_OPERATOR_APPROVED = "NOT_OPERATOR_APPROVED"
    ARTIFACT_CANCELLED = "ARTIFACT_CANCELLED"
    APPROVAL_MISSING = "APPROVAL_MISSING"
    APPROVAL_MISMATCH = "APPROVAL_MISMATCH"
    CONTENT_INTEGRITY_FAILED = "CONTENT_INTEGRITY_FAILED"
    RECIPIENT_MISMATCH = "RECIPIENT_MISMATCH"
    SENDER_NOT_ALLOWED = "SENDER_NOT_ALLOWED"
    RETRY_NOT_PERMITTED = "RETRY_NOT_PERMITTED"
    RETRY_LIMIT_REACHED = "RETRY_LIMIT_REACHED"
    ATTEMPT_UNRESOLVED = "ATTEMPT_UNRESOLVED"
    ALREADY_ACCEPTED = "ALREADY_ACCEPTED"
    FINALIZATION_FAILED = "FINALIZATION_FAILED"
    TRANSPORT_EXCEPTION = "TRANSPORT_EXCEPTION"
    RECONCILIATION_UNAVAILABLE = "RECONCILIATION_UNAVAILABLE"
    # A result arrived for an attempt that was already resolved and contradicts it.
    LATE_RESULT_CONFLICT = "LATE_RESULT_CONFLICT"
    # An attempt of this message has durable late acceptance evidence: no further attempt.
    ACCEPTANCE_EVIDENCE_CONFLICT = "ACCEPTANCE_EVIDENCE_CONFLICT"


class DispatchResult(CoreModel):
    outbound_id: EntityId
    outcome: DispatchOutcome
    # The message's status after this call, read from persistence.
    outbound_status: OutboundStatus
    reason_codes: tuple[NonEmptyStr, ...] = ()
    attempt_id: EntityId | None = None
    attempt_no: int | None = None
    permit_id: EntityId | None = None
    reservation_id: EntityId | None = None
    provider_message_id: NonEmptyStr | None = None
    recipient: EmailAddress | None = None
    correlation_id: EntityId
    occurred_at: AwareDatetime
    # True when this call returned an already-recorded outcome without a transport call.
    replayed: bool = False
    # True when the outcome came from reconciliation, not from a submission.
    reconciled: bool = False
    transport_called: bool = False


class AttemptView(CoreModel):
    """One dispatch attempt as recorded (history is never rewritten)."""

    attempt_id: EntityId
    outbound_id: EntityId
    attempt_no: int
    state: str
    reason_code: str | None
    claimed_at: AwareDatetime
    provider_message_id: str | None = None
    late_acceptance_provider_message_id: str | None = None


class DispatchStatusView(CoreModel):
    """Current dispatch state of one message and its full attempt history.

    ``acceptance_conflict``: some attempt recorded as NOT_ACCEPTED has later positive
    acceptance evidence. The message is then never attempted again; an operator decides
    what (if anything) to do. It does not state how many emails the recipient received."""

    outbound_id: EntityId
    outbound_status: OutboundStatus
    attempts: tuple[AttemptView, ...]
    unresolved: bool
    acceptance_conflict: bool
