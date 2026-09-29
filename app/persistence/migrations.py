"""Minimal deterministic schema migrations.

Each migration runs in its own IMMEDIATE transaction together with the insert of its
``schema_version`` row, so a migration is either fully applied or not at all. The
current version is re-read inside that transaction, so two processes initializing the
same database cannot apply a migration twice.
"""

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from app.persistence.clock import Clock
from app.persistence.errors import SchemaVersionError
from app.persistence.schema import V1_INITIAL_SCHEMA
from app.persistence.serialization import to_utc_text

_CREATE_VERSION_TABLE = """CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL
) STRICT"""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    apply: Callable[[sqlite3.Connection], None]


def v1_initial_schema(connection: sqlite3.Connection) -> None:
    # Statements run one by one: executescript() would commit the open transaction.
    for statement in V1_INITIAL_SCHEMA:
        connection.execute(statement)


MIGRATIONS: tuple[Migration, ...] = (Migration(1, "initial_schema", v1_initial_schema),)


def latest_version(migrations: tuple[Migration, ...] = MIGRATIONS) -> int:
    return migrations[-1].version if migrations else 0


def validate_migration_order(migrations: tuple[Migration, ...]) -> None:
    expected = list(range(1, len(migrations) + 1))
    if [migration.version for migration in migrations] != expected:
        raise SchemaVersionError("migrations must be numbered 1..N without gaps, in order")


def current_version(connection: sqlite3.Connection) -> int:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'"
    ).fetchone()
    if exists is None:
        return 0
    row = connection.execute("SELECT MAX(version) FROM schema_version").fetchone()
    return int(row[0]) if row[0] is not None else 0


def apply_migrations(
    connection: sqlite3.Connection,
    clock: Clock,
    migrations: tuple[Migration, ...] = MIGRATIONS,
) -> int:
    """Apply all pending migrations and return the resulting schema version."""
    validate_migration_order(migrations)
    _run_in_transaction(connection, lambda: connection.execute(_CREATE_VERSION_TABLE))
    supported = latest_version(migrations)
    if current_version(connection) > supported:
        raise SchemaVersionError(
            f"database schema version {current_version(connection)} is newer than supported {supported}"
        )
    for migration in migrations:
        _run_in_transaction(connection, lambda m=migration: _apply_if_pending(connection, m, clock))
    return current_version(connection)


def _apply_if_pending(connection: sqlite3.Connection, migration: Migration, clock: Clock) -> None:
    if current_version(connection) >= migration.version:
        return
    migration.apply(connection)
    connection.execute(
        "INSERT INTO schema_version (version, name, applied_at) VALUES (?, ?, ?)",
        (migration.version, migration.name, to_utc_text(clock.now())),
    )


def _run_in_transaction(connection: sqlite3.Connection, work: Callable[[], object]) -> None:
    connection.execute("BEGIN IMMEDIATE")
    try:
        work()
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    connection.execute("COMMIT")
