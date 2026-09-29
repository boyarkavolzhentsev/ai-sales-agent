from typing import Annotated, Self

from pydantic import AfterValidator, AwareDatetime, model_validator

from app.core.enums import ConfidenceBand, LeadIntent, LeadStage, RiskFlag
from app.core.models.base import CoreModel
from app.core.models.types import EntityId, LocaleTag, UniqueNonEmptyStrs
from app.core.validation import unique_items


class IntentClassification(CoreModel):
    """LLM interpretation of one inbound message. A proposal, never authoritative state."""

    message_id: EntityId
    primary_intent: LeadIntent
    secondary_intents: Annotated[tuple[LeadIntent, ...], AfterValidator(unique_items)] = ()
    extracted_questions: UniqueNonEmptyStrs = ()
    proposed_stage: LeadStage | None = None
    confidence_band: ConfidenceBand
    risk_flags: Annotated[tuple[RiskFlag, ...], AfterValidator(unique_items)] = ()
    language: LocaleTag
    created_at: AwareDatetime

    @model_validator(mode="after")
    def _check_intents(self) -> Self:
        if self.primary_intent in self.secondary_intents:
            raise ValueError("primary_intent must not be repeated in secondary_intents")
        return self
