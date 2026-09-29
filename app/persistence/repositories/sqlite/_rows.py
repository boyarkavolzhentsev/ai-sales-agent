"""Shared row mapping and update-outcome helpers for the SQLite repositories."""

import sqlite3
from collections.abc import Iterable, Sequence
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from app.persistence.errors import ConcurrencyError, CorruptRecordError, NotFoundError
from app.persistence.serialization import model_from_json
from app.persistence.transaction import SqlValue, Transaction

M = TypeVar("M", bound=BaseModel)


def load_row(model_type: type[M], row: sqlite3.Row) -> M:
    try:
        return model_from_json(model_type, row["data"])
    except ValidationError as exc:
        raise CorruptRecordError(f"stored {model_type.__name__} failed validation") from exc


def load(model_type: type[M], row: sqlite3.Row | None) -> M | None:
    return None if row is None else load_row(model_type, row)


def load_all(model_type: type[M], rows: Iterable[sqlite3.Row]) -> list[M]:
    return [load_row(model_type, row) for row in rows]


def require_next_version(new_version: int, expected_version: int) -> None:
    """A versioned update must carry exactly ``expected_version + 1``."""
    if expected_version < 1 or new_version != expected_version + 1:
        raise ValueError(
            f"entity version must be expected_version + 1 "
            f"(got version={new_version}, expected_version={expected_version})"
        )


def ensure_updated(
    cursor: sqlite3.Cursor,
    tx: Transaction,
    exists_sql: str,
    key: Sequence[SqlValue],
    description: str,
) -> None:
    """Turn a zero-row conditional UPDATE into NotFoundError or ConcurrencyError."""
    if cursor.rowcount == 1:
        return
    if tx.fetch_one(exists_sql, key) is None:
        raise NotFoundError(f"{description} does not exist")
    raise ConcurrencyError(f"{description} was modified concurrently")
