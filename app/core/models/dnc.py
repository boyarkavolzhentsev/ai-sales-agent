from typing import Self

from pydantic import AwareDatetime, ValidationInfo, field_validator, model_validator

from app.core.enums import DNCReason, DNCScope
from app.core.models.base import CoreModel
from app.core.models.refs import EntityRef
from app.core.models.types import EntityId, NonEmptyStr
from app.core.validation import ensure_after, normalize_domain, normalize_email


class DoNotContactEntry(CoreModel):
    """Suppression entry. The DNC registry is the only source of suppression truth.

    ``value`` is normalized according to ``scope``: a bare email for EMAIL, a
    domain for DOMAIN. ``expires_at=None`` means permanent.
    """

    entry_id: EntityId
    scope: DNCScope
    value: str
    reason: DNCReason
    source_ref: EntityRef
    created_by: NonEmptyStr
    created_at: AwareDatetime
    expires_at: AwareDatetime | None = None

    @field_validator("value")
    @classmethod
    def _normalize_value(cls, value: str, info: ValidationInfo) -> str:
        scope = info.data.get("scope")
        if scope is DNCScope.EMAIL:
            return normalize_email(value)
        if scope is DNCScope.DOMAIN:
            return normalize_domain(value)
        # scope itself failed validation; that error is reported on its own field.
        return value

    @model_validator(mode="after")
    def _check_expiry(self) -> Self:
        ensure_after(self.expires_at, self.created_at, "expires_at", "created_at")
        return self
