from datetime import datetime

from app.persistence.errors import AlreadyExistsError, DuplicateIdempotencyKeyError, PersistenceError
from app.persistence.records import IdempotencyRecord
from app.persistence.serialization import from_utc_text, to_utc_text
from app.persistence.transaction import Transaction


class SqliteIdempotencyRepository:
    """Idempotency key reservations. Infrastructure only: no operation is executed here.

    A duplicate ``reserve`` never creates a second record; it raises
    DuplicateIdempotencyKeyError carrying the original reservation.
    """

    def __init__(self, tx: Transaction) -> None:
        self._tx = tx

    def reserve(self, key: str, operation: str, created_at: datetime) -> IdempotencyRecord:
        record = IdempotencyRecord(key=key, operation=operation, created_at=created_at)
        try:
            self._tx.execute(
                "INSERT INTO idempotency_keys (key, operation, created_at) VALUES (?, ?, ?)",
                (record.key, record.operation, to_utc_text(record.created_at)),
            )
        except AlreadyExistsError as exc:
            existing = self.get(key)
            if existing is None:
                raise PersistenceError(f"idempotency key conflict without a record: {key!r}") from exc
            raise DuplicateIdempotencyKeyError(existing) from exc
        return record

    def exists(self, key: str) -> bool:
        return self._tx.fetch_one("SELECT 1 FROM idempotency_keys WHERE key = ?", (key,)) is not None

    def get(self, key: str) -> IdempotencyRecord | None:
        row = self._tx.fetch_one(
            "SELECT key, operation, created_at FROM idempotency_keys WHERE key = ?", (key,)
        )
        if row is None:
            return None
        return IdempotencyRecord(
            key=row["key"], operation=row["operation"], created_at=from_utc_text(row["created_at"])
        )
