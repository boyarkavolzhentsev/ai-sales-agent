"""Persistence-infrastructure records that have no core-domain counterpart."""

from pydantic import AwareDatetime

from app.core.models.base import CoreModel
from app.core.models.types import NonEmptyStr


class IdempotencyRecord(CoreModel):
    """A reserved idempotency key and the operation that reserved it."""

    key: NonEmptyStr
    operation: NonEmptyStr
    created_at: AwareDatetime
