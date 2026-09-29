"""Persistence-infrastructure records that have no core-domain counterpart."""

from datetime import date
from enum import StrEnum
from typing import Self

from pydantic import AwareDatetime, model_validator

from app.core.enums import OutboundKind, OutboundStatus
from app.core.models.base import CoreModel
from app.core.models.types import EmailAddress, EntityId, NonEmptyStr, Version
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
