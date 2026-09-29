from typing import Self

from pydantic import AwareDatetime, model_validator

from app.core.enums import CloseReason, LeadIntent, LeadOrigin, LeadStage, LeadStatus
from app.core.models.base import CoreModel
from app.core.models.types import EntityId, Version
from app.core.validation import ensure_not_before


class Lead(CoreModel):
    """One sales opportunity with one contact.

    ``stage``, ``status`` and ``close_reason`` are authoritative and may only change
    through validated transitions (see ``is_allowed_lead_transition``).
    """

    lead_id: EntityId
    contact_id: EntityId
    company_id: EntityId
    origin: LeadOrigin
    campaign_id: EntityId | None = None
    stage: LeadStage
    status: LeadStatus = LeadStatus.AUTOMATED
    close_reason: CloseReason | None = None
    last_intent: LeadIntent | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime
    version: Version = 1

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        if self.stage is LeadStage.CLOSED and self.close_reason is None:
            raise ValueError("a CLOSED lead requires close_reason")
        if self.stage is not LeadStage.CLOSED and self.close_reason is not None:
            raise ValueError("close_reason is only allowed on a CLOSED lead")
        ensure_not_before(self.updated_at, self.created_at, "updated_at", "created_at")
        return self
