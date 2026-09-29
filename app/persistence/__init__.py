"""SQLite-first persistence foundation: database lifecycle, migrations, transactions,
repositories, serialization and the Clock abstraction. No business workflows."""

from app.persistence.clock import Clock, FrozenClock, SystemClock
from app.persistence.database import MEMORY, Database
from app.persistence.errors import (
    AlreadyExistsError,
    ConcurrencyError,
    CorruptRecordError,
    DuplicateIdempotencyKeyError,
    IntegrityError,
    NotFoundError,
    PersistenceError,
    SchemaVersionError,
)
from app.persistence.records import IdempotencyRecord
from app.persistence.unit_of_work import UnitOfWork

__all__ = [
    "MEMORY",
    "AlreadyExistsError",
    "Clock",
    "ConcurrencyError",
    "CorruptRecordError",
    "Database",
    "DuplicateIdempotencyKeyError",
    "FrozenClock",
    "IdempotencyRecord",
    "IntegrityError",
    "NotFoundError",
    "PersistenceError",
    "SchemaVersionError",
    "SystemClock",
    "UnitOfWork",
]
