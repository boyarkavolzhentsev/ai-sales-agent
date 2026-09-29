"""Low-level handle to one active SQLite transaction.

Repositories execute SQL only through a ``Transaction``. The handle refuses to run
statements once its transaction has ended, so nothing can silently autocommit
outside the transaction boundary. All sqlite3 errors are translated here.
"""

import sqlite3
from collections.abc import Sequence

from app.persistence.errors import AlreadyExistsError, IntegrityError, PersistenceError

SqlValue = str | int | float | bytes | None

_UNIQUE_ERROR_NAMES = frozenset({"SQLITE_CONSTRAINT_UNIQUE", "SQLITE_CONSTRAINT_PRIMARYKEY"})


def translate_sqlite_error(exc: sqlite3.Error) -> PersistenceError:
    if isinstance(exc, sqlite3.IntegrityError):
        if exc.sqlite_errorname in _UNIQUE_ERROR_NAMES:
            return AlreadyExistsError(str(exc))
        return IntegrityError(str(exc))
    return PersistenceError(f"{type(exc).__name__}: {exc}")


class Transaction:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._active = True

    @property
    def active(self) -> bool:
        return self._active

    def execute(self, sql: str, params: Sequence[SqlValue] = ()) -> sqlite3.Cursor:
        if not self._active:
            raise PersistenceError("transaction is no longer active")
        try:
            return self._connection.execute(sql, params)
        except sqlite3.Error as exc:
            raise translate_sqlite_error(exc) from exc

    def fetch_one(self, sql: str, params: Sequence[SqlValue] = ()) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self.execute(sql, params).fetchone()
        return row

    def fetch_all(self, sql: str, params: Sequence[SqlValue] = ()) -> list[sqlite3.Row]:
        rows: list[sqlite3.Row] = self.execute(sql, params).fetchall()
        return rows

    def close(self) -> None:
        """Called by the owning Database when the transaction commits or rolls back."""
        self._active = False
