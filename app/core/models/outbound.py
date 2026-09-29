from collections.abc import Mapping
from typing import Self

from pydantic import AwareDatetime, NonNegativeInt, model_validator

from app.core.enums import OutboundDecision, OutboundKind, OutboundStatus
from app.core.models.base import CoreModel
from app.core.models.types import EntityId, NonEmptyStr, Sha256Hex, UniqueNonEmptyStrs, Version
from app.core.validation import ensure_not_before

_SEND_ONLY: frozenset[OutboundDecision | None] = frozenset({OutboundDecision.SEND})

# Which decisions are consistent with each status. None means "not yet decided".
_ALLOWED_DECISIONS: Mapping[OutboundStatus, frozenset[OutboundDecision | None]] = {
    OutboundStatus.DRAFTED: frozenset({None, OutboundDecision.ESCALATE}),
    OutboundStatus.PENDING_REVIEW: _SEND_ONLY,
    OutboundStatus.HELD: frozenset({OutboundDecision.HOLD}),
    OutboundStatus.APPROVED: _SEND_ONLY,
    OutboundStatus.SENDING: _SEND_ONLY,
    OutboundStatus.SENT: _SEND_ONLY,
    OutboundStatus.FAILED: _SEND_ONLY,
    OutboundStatus.BOUNCED: _SEND_ONLY,
    OutboundStatus.SKIPPED: frozenset({OutboundDecision.SKIP}),
    OutboundStatus.CANCELLED: frozenset({None, *OutboundDecision}),
}

_PERMITTED = frozenset(
    {
        OutboundStatus.APPROVED,
        OutboundStatus.SENDING,
        OutboundStatus.SENT,
        OutboundStatus.FAILED,
        OutboundStatus.BOUNCED,
    }
)
_DISPATCHED = frozenset(
    {OutboundStatus.SENDING, OutboundStatus.SENT, OutboundStatus.FAILED, OutboundStatus.BOUNCED}
)
_DELIVERED = frozenset({OutboundStatus.SENT, OutboundStatus.BOUNCED})


class OutboundMessage(CoreModel):
    """Intent-to-send and send-ledger entry for every email the system sends.

    ``status`` and ``sent_at`` are authoritative for counts, limits and statistics.
    """

    outbound_id: EntityId
    idempotency_key: NonEmptyStr
    kind: OutboundKind
    lead_id: EntityId
    contact_id: EntityId
    campaign_id: EntityId | None = None
    thread_id: EntityId | None = None
    sequence_no: NonNegativeInt
    draft_id: EntityId
    subject: NonEmptyStr
    body_final: NonEmptyStr
    content_hash: Sha256Hex
    decision: OutboundDecision | None = None
    decision_reasons: UniqueNonEmptyStrs = ()
    status: OutboundStatus = OutboundStatus.DRAFTED
    send_permit_id: EntityId | None = None
    hold_reason: NonEmptyStr | None = None
    provider_message_id: NonEmptyStr | None = None
    rfc_message_id: NonEmptyStr | None = None
    failure_reason: NonEmptyStr | None = None
    created_at: AwareDatetime
    approved_at: AwareDatetime | None = None
    sending_at: AwareDatetime | None = None
    sent_at: AwareDatetime | None = None
    # Optimistic-concurrency version; incremented by exactly 1 on every persisted update.
    version: Version = 1

    @model_validator(mode="after")
    def _check_kind(self) -> Self:
        if self.kind in (OutboundKind.FIRST_TOUCH, OutboundKind.FOLLOW_UP) and self.campaign_id is None:
            raise ValueError(f"{self.kind} requires campaign_id")
        if self.kind in (OutboundKind.FOLLOW_UP, OutboundKind.REPLY) and self.thread_id is None:
            raise ValueError(f"{self.kind} requires thread_id")
        if self.kind is OutboundKind.FIRST_TOUCH and self.sequence_no != 0:
            raise ValueError("FIRST_TOUCH must have sequence_no 0")
        if self.kind is OutboundKind.FOLLOW_UP and self.sequence_no < 1:
            raise ValueError("FOLLOW_UP must have sequence_no >= 1")
        return self

    @model_validator(mode="after")
    def _check_status(self) -> Self:
        if self.decision not in _ALLOWED_DECISIONS[self.status]:
            raise ValueError(f"decision {self.decision} is inconsistent with status {self.status}")
        if self.status in _PERMITTED and (self.send_permit_id is None or self.approved_at is None):
            raise ValueError(f"status {self.status} requires send_permit_id and approved_at")
        if self.status in _DISPATCHED and self.sending_at is None:
            raise ValueError(f"status {self.status} requires sending_at")
        if (self.sent_at is not None) != (self.status in _DELIVERED):
            raise ValueError("sent_at is required for, and only allowed on, SENT/BOUNCED")
        if (self.hold_reason is not None) != (self.status is OutboundStatus.HELD):
            raise ValueError("hold_reason is required for, and only allowed on, HELD")
        if self.status is OutboundStatus.FAILED and self.failure_reason is None:
            raise ValueError("status FAILED requires failure_reason")
        return self

    @model_validator(mode="after")
    def _check_timestamps(self) -> Self:
        ensure_not_before(self.approved_at, self.created_at, "approved_at", "created_at")
        ensure_not_before(self.sending_at, self.approved_at, "sending_at", "approved_at")
        ensure_not_before(self.sent_at, self.sending_at, "sent_at", "sending_at")
        ensure_not_before(self.sent_at, self.created_at, "sent_at", "created_at")
        return self
