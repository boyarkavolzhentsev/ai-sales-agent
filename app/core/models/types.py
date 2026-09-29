"""Shared constrained types for core contracts.

Datetimes use pydantic's ``AwareDatetime`` everywhere, which rejects naive values.
"""

from typing import Annotated

from pydantic import AfterValidator, Field, JsonValue, StringConstraints

from app.core.validation import normalize_domain, normalize_email, require_non_blank, unique_items

# UUID- and ULID-compatible opaque identifier (letters, digits, '-', '_').
EntityId = Annotated[
    str,
    StringConstraints(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$"),
]

NonEmptyStr = Annotated[str, AfterValidator(require_non_blank)]

EmailAddress = Annotated[str, AfterValidator(normalize_email)]

DomainName = Annotated[str, AfterValidator(normalize_domain)]

# Lower-case hex SHA-256 digest. Upper-case is rejected, not folded.
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]

# BCP 47-style language tag, e.g. "en", "en-GB", "uk".
LocaleTag = Annotated[str, StringConstraints(pattern=r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$")]

# ISO 3166-1 alpha-2 country code, upper-case.
CountryCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{2}$")]

Version = Annotated[int, Field(ge=1)]

UniqueEntityIds = Annotated[tuple[EntityId, ...], AfterValidator(unique_items)]
UniqueNonEmptyStrs = Annotated[tuple[NonEmptyStr, ...], AfterValidator(unique_items)]
UniqueEmailAddresses = Annotated[tuple[EmailAddress, ...], AfterValidator(unique_items)]
UniqueSha256 = Annotated[tuple[Sha256Hex, ...], AfterValidator(unique_items)]

# Free-form JSON object for display payloads and audit snapshots, whose shape varies by
# event or command type; JsonValue keeps it serializable without resorting to Any.
JsonObject = dict[str, JsonValue]
