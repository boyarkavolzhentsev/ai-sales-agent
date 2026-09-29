from typing import Annotated, Self

from pydantic import AwareDatetime, Field, model_validator

from app.core.enums import EmailDirection
from app.core.models.base import CoreModel
from app.core.models.types import (
    EmailAddress,
    EntityId,
    NonEmptyStr,
    Sha256Hex,
    UniqueEmailAddresses,
    UniqueEntityIds,
)


class EmailMessage(CoreModel):
    """One received or sent email, stored as observed. Immutable."""

    message_id: EntityId
    rfc_message_id: NonEmptyStr
    thread_id: EntityId
    direction: EmailDirection
    mailbox: EmailAddress
    from_address: EmailAddress
    to_addresses: UniqueEmailAddresses = ()
    cc_addresses: UniqueEmailAddresses = ()
    subject: str
    body_text: str
    raw_ref: NonEmptyStr
    raw_hash: Sha256Hex
    in_reply_to: NonEmptyStr | None = None
    # Kept exactly as observed in the header, including any duplicates.
    references: tuple[NonEmptyStr, ...] = ()
    # Raw header values; interpretation happens in the pre-filter.
    auto_submitted: NonEmptyStr | None = None
    list_unsubscribe: NonEmptyStr | None = None
    received_at: AwareDatetime | None = None
    sent_at: AwareDatetime | None = None
    is_bounce: bool = False
    is_auto_generated: bool = False

    @model_validator(mode="after")
    def _check_direction_timestamps(self) -> Self:
        if self.direction is EmailDirection.INBOUND and self.received_at is None:
            raise ValueError("inbound email requires received_at")
        if self.direction is EmailDirection.OUTBOUND and self.sent_at is None:
            raise ValueError("outbound email requires sent_at")
        return self


class EmailThread(CoreModel):
    """A conversation grouping built deterministically from email headers."""

    thread_id: EntityId
    mailbox: EmailAddress
    participant_addresses: Annotated[UniqueEmailAddresses, Field(min_length=1)]
    subject_normalized: str
    lead_id: EntityId | None = None
    message_ids: UniqueEntityIds = ()
    last_inbound_at: AwareDatetime | None = None
    last_outbound_at: AwareDatetime | None = None
