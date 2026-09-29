from typing import Self

from pydantic import AwareDatetime, model_validator

from app.core.enums import ContactDepartment, ContactSource, ContactType, EmailValidity, IcpFit
from app.core.models.base import CoreModel
from app.core.models.types import (
    CountryCode,
    DomainName,
    EmailAddress,
    EntityId,
    LocaleTag,
    NonEmptyStr,
    Version,
)
from app.core.validation import ensure_not_before


class ProspectCompany(CoreModel):
    """An organisation being sold to. ``domain`` is its identity; ``icp_fit`` is derived."""

    company_id: EntityId
    name: NonEmptyStr
    domain: DomainName
    industry: NonEmptyStr | None = None
    size_band: NonEmptyStr | None = None
    country: CountryCode | None = None
    icp_fit: IcpFit = IcpFit.UNKNOWN
    icp_reason: NonEmptyStr | None = None
    icp_assessed_at: AwareDatetime | None = None
    source: ContactSource
    source_ref: NonEmptyStr | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime
    # Optimistic-concurrency version; incremented by exactly 1 on every persisted update.
    version: Version = 1

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        if self.icp_fit is not IcpFit.UNKNOWN and self.icp_assessed_at is None:
            raise ValueError("a FIT/NOT_FIT assessment requires icp_assessed_at")
        ensure_not_before(self.updated_at, self.created_at, "updated_at", "created_at")
        return self


class ProspectContact(CoreModel):
    """One business email address at a prospect company.

    The email and its provenance (source, source_ref, collected_at) are authoritative.
    Contactability is never cached here; it is always checked against the DNC registry.
    """

    contact_id: EntityId
    company_id: EntityId
    email: EmailAddress
    name: NonEmptyStr | None = None
    role_title: NonEmptyStr | None = None
    department: ContactDepartment
    contact_type: ContactType
    source: ContactSource
    source_ref: NonEmptyStr | None = None
    collected_at: AwareDatetime
    email_validity: EmailValidity = EmailValidity.UNKNOWN
    # IANA zone name; not checked against a tz database in Stage 1.
    timezone: NonEmptyStr | None = None
    locale: LocaleTag | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime
    # Optimistic-concurrency version; incremented by exactly 1 on every persisted update.
    version: Version = 1

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        ensure_not_before(self.updated_at, self.created_at, "updated_at", "created_at")
        return self
