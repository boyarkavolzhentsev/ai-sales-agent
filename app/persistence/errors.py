"""Typed persistence errors. Raw sqlite3 errors are translated into these at the boundary."""

from app.persistence.records import IdempotencyRecord


class PersistenceError(Exception):
    """Base class for all persistence failures."""


class NotFoundError(PersistenceError):
    """The addressed record does not exist."""


class AlreadyExistsError(PersistenceError):
    """A primary key or unique identity is already taken."""


class ConcurrencyError(PersistenceError):
    """An optimistic-concurrency check failed: the record changed since it was read."""


class IntegrityError(PersistenceError):
    """A foreign key, check constraint or append-only guard rejected the write."""


class SchemaVersionError(PersistenceError):
    """The database schema version is unknown or newer than this code supports."""


class CorruptRecordError(PersistenceError):
    """A stored record no longer validates against its core contract."""


class DuplicateIdempotencyKeyError(AlreadyExistsError):
    """An idempotency key was reserved twice. ``existing`` is the original reservation."""

    def __init__(self, existing: IdempotencyRecord) -> None:
        super().__init__(f"idempotency key already reserved: {existing!r}")
        self.existing = existing
