from typing import Annotated, Self

from pydantic import AwareDatetime, Field, model_validator

from app.core.models.base import CoreModel
from app.core.models.types import EntityId, NonEmptyStr, Sha256Hex, UniqueNonEmptyStrs
from app.core.validation import ensure_after, ensure_not_before


class SendPermit(CoreModel):
    """Proof that the policy gate approved one exact message (bound by ``content_hash``).

    Conceptually immutable and single-use: ``consumed_at`` is set once, by the send
    gate, within the validity window. Consumption logic is not implemented in Stage 1.
    """

    permit_id: EntityId
    outbound_id: EntityId
    content_hash: Sha256Hex
    checks_passed: Annotated[UniqueNonEmptyStrs, Field(min_length=1)]
    policy_config_version: NonEmptyStr
    issued_at: AwareDatetime
    expires_at: AwareDatetime
    consumed_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _check_window(self) -> Self:
        ensure_after(self.expires_at, self.issued_at, "expires_at", "issued_at")
        ensure_not_before(self.consumed_at, self.issued_at, "consumed_at", "issued_at")
        ensure_not_before(self.expires_at, self.consumed_at, "expires_at", "consumed_at")
        return self
