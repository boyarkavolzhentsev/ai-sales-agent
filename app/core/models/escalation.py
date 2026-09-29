from typing import Annotated, Self

from pydantic import AfterValidator, AwareDatetime, Field, model_validator

from app.core.enums import (
    EscalationReason,
    EscalationResolution,
    EscalationSeverity,
    EscalationStatus,
)
from app.core.models.base import CoreModel
from app.core.models.refs import EntityRef
from app.core.models.types import EntityId, NonEmptyStr, Version
from app.core.validation import ensure_not_before, unique_items


class Escalation(CoreModel):
    """A case handed to the operator. While OPEN, automation on the lead is on hold."""

    escalation_id: EntityId
    lead_id: EntityId
    trigger_ref: EntityRef
    reasons: Annotated[
        tuple[EscalationReason, ...], Field(min_length=1), AfterValidator(unique_items)
    ]
    severity: EscalationSeverity = EscalationSeverity.NORMAL
    # LLM-generated summary; always presented to the operator as such.
    summary: NonEmptyStr | None = None
    suggested_action: NonEmptyStr | None = None
    status: EscalationStatus = EscalationStatus.OPEN
    resolution: EscalationResolution | None = None
    created_at: AwareDatetime
    resolved_at: AwareDatetime | None = None
    resolved_by: NonEmptyStr | None = None
    telegram_ref: NonEmptyStr | None = None
    # Optimistic-concurrency version; incremented by exactly 1 on every persisted update.
    version: Version = 1

    @model_validator(mode="after")
    def _check_resolution(self) -> Self:
        resolution_fields = (self.resolution, self.resolved_at, self.resolved_by)
        if self.status is EscalationStatus.RESOLVED:
            if any(field is None for field in resolution_fields):
                raise ValueError("a RESOLVED escalation requires resolution, resolved_at, resolved_by")
        elif any(field is not None for field in resolution_fields):
            raise ValueError("resolution fields are only allowed on a RESOLVED escalation")
        ensure_not_before(self.resolved_at, self.created_at, "resolved_at", "created_at")
        return self
