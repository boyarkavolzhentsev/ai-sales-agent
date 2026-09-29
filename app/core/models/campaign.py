from datetime import timedelta
from typing import Annotated, Self

from pydantic import AfterValidator, AwareDatetime, Field, NonNegativeInt, model_validator

from app.core.enums import CampaignReviewMode, CampaignStatus, ContactDepartment, KnowledgeDomain
from app.core.models.base import CoreModel
from app.core.models.types import (
    CountryCode,
    EmailAddress,
    EntityId,
    NonEmptyStr,
    UniqueNonEmptyStrs,
    Version,
)
from app.core.validation import ensure_after, ensure_not_before, unique_items


class CampaignTargetFilter(CoreModel):
    """Which prospects a campaign targets. Empty tuples mean "no restriction on this field"."""

    industries: UniqueNonEmptyStrs = ()
    countries: Annotated[tuple[CountryCode, ...], AfterValidator(unique_items)] = ()
    size_bands: UniqueNonEmptyStrs = ()
    departments: Annotated[tuple[ContactDepartment, ...], AfterValidator(unique_items)] = ()


class Campaign(CoreModel):
    """An operator-defined outreach programme.

    ``review_mode`` accepts every CampaignReviewMode value, but only EACH is
    supported behaviourally in V1 (see ``is_review_mode_supported_v1``).
    """

    campaign_id: EntityId
    name: NonEmptyStr
    status: CampaignStatus = CampaignStatus.DRAFT
    target_filter: CampaignTargetFilter
    allowed_knowledge_domains: Annotated[
        tuple[KnowledgeDomain, ...], Field(min_length=1), AfterValidator(unique_items)
    ]
    sending_mailbox: EmailAddress
    max_follow_ups: NonNegativeInt
    min_interval_between_follow_ups: timedelta
    review_mode: CampaignReviewMode = CampaignReviewMode.EACH
    start_at: AwareDatetime | None = None
    end_at: AwareDatetime | None = None
    created_by: NonEmptyStr
    activated_by: NonEmptyStr | None = None
    config_version: Version = 1
    created_at: AwareDatetime
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        if self.min_interval_between_follow_ups <= timedelta(0):
            raise ValueError("min_interval_between_follow_ups must be positive")
        if self.status is not CampaignStatus.DRAFT and self.activated_by is None:
            raise ValueError("a campaign that left DRAFT requires activated_by")
        ensure_after(self.end_at, self.start_at, "end_at", "start_at")
        ensure_not_before(self.updated_at, self.created_at, "updated_at", "created_at")
        return self
