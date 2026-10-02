"""Explicit SQLite database lifecycle. No hidden global connection.

One ``Database`` owns one connection, opened with ``connect()`` (or ``with``) and closed
with ``close()``. The connection runs in autocommit mode at the driver level so that
every transaction is explicit: ``transaction()`` issues BEGIN IMMEDIATE and commits on
success or rolls back on any exception.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType
from typing import Self

from app.persistence.clock import Clock
from app.persistence.errors import PersistenceError
from app.persistence.migrations import apply_migrations, current_version
from app.persistence.transaction import Transaction, translate_sqlite_error
from app.persistence.unit_of_work import UnitOfWork

MEMORY = ":memory:"
# STRICT tables need 3.37; JSON functions are built in from 3.38.
MIN_SQLITE_VERSION = (3, 38, 0)


class Database:
    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 5000) -> None:
        if busy_timeout_ms < 0:
            raise ValueError("busy_timeout_ms must not be negative")
        self._path = str(path)
        self._busy_timeout_ms = busy_timeout_ms
        self._connection: sqlite3.Connection | None = None

    @property
    def path(self) -> str:
        return self._path

    @property
    def is_open(self) -> bool:
        return self._connection is not None

    def connect(self) -> None:
        if self._connection is not None:
            raise PersistenceError("database is already connected")
        if sqlite3.sqlite_version_info < MIN_SQLITE_VERSION:
            raise PersistenceError(f"SQLite {sqlite3.sqlite_version} is older than required 3.38")
        try:
            connection = sqlite3.connect(
                self._path, timeout=self._busy_timeout_ms / 1000, isolation_level=None
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
        except sqlite3.Error as exc:
            raise translate_sqlite_error(exc) from exc
        if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            connection.close()
            raise PersistenceError("SQLite foreign key enforcement could not be enabled")
        self._connection = connection

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def __enter__(self) -> Self:
        self.connect()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def initialize_schema(self, clock: Clock) -> int:
        """Apply pending migrations; safe to call repeatedly. Returns the schema version."""
        connection = self._require_connection()
        try:
            return apply_migrations(connection, clock)
        except sqlite3.Error as exc:
            raise translate_sqlite_error(exc) from exc

    def schema_version(self) -> int:
        try:
            return current_version(self._require_connection())
        except sqlite3.Error as exc:
            raise translate_sqlite_error(exc) from exc

    @contextmanager
    def transaction(self) -> Iterator[UnitOfWork]:
        """One atomic unit of work. Nested transactions are rejected."""
        connection = self._require_connection()
        if connection.in_transaction:
            raise PersistenceError("nested transactions are not supported")
        try:
            connection.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            raise translate_sqlite_error(exc) from exc
        tx = Transaction(connection)
        try:
            yield UnitOfWork(tx)
        except BaseException:
            tx.close()
            connection.execute("ROLLBACK")
            raise
        tx.close()
        try:
            connection.execute("COMMIT")
        except sqlite3.Error as exc:
            connection.execute("ROLLBACK")
            raise translate_sqlite_error(exc) from exc

    @staticmethod
    @contextmanager
    def read_only(path: str | Path) -> Iterator[UnitOfWork]:
        """A read-only snapshot of an existing database file (``mode=ro``): repositories can
        read, every write fails, nothing is created or migrated. For operational checks."""
        try:
            connection = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN")  # deferred: a consistent snapshot without taking a write lock
        except sqlite3.Error as exc:
            raise translate_sqlite_error(exc) from exc
        tx = Transaction(connection)
        try:
            yield UnitOfWork(tx)
        finally:
            tx.close()
            connection.execute("ROLLBACK")
            connection.close()

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise PersistenceError("database is not connected")
        return self._connection
