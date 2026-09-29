import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from app.persistence import (
    MEMORY,
    Database,
    FrozenClock,
    IntegrityError,
    PersistenceError,
    SchemaVersionError,
)
from app.persistence.migrations import (
    MIGRATIONS,
    Migration,
    apply_migrations,
    current_version,
    latest_version,
    validate_migration_order,
)
from app.persistence.serialization import to_utc_text
from tests.persistence import factories as f

EXPECTED_TABLES = {
    "companies",
    "contacts",
    "leads",
    "email_threads",
    "email_messages",
    "campaigns",
    "outbound_messages",
    "follow_up_plans",
    "do_not_contact",
    "escalations",
    "operator_commands",
    "operator_responses",
    "audit_events",
    "audit_event_subjects",
    "provenance_records",
    "knowledge_sources_meta",
    "idempotency_keys",
    "schema_version",
}


def _raw(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path, isolation_level=None)


# ---- Schema -------------------------------------------------------------------


def test_empty_database_initializes(db_path: Path, clock: FrozenClock) -> None:
    with Database(db_path) as db:
        assert db.schema_version() == 0
        assert db.initialize_schema(clock) == latest_version()
        assert db.schema_version() == latest_version() == len(MIGRATIONS)
    raw = _raw(db_path)
    try:
        tables = {row[0] for row in raw.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        raw.close()
    assert EXPECTED_TABLES <= tables


def test_second_initialize_is_safe_and_migration_applies_once(db_path: Path, clock: FrozenClock) -> None:
    with Database(db_path) as db:
        db.initialize_schema(clock)
        clock.advance(timedelta(hours=1))
        assert db.initialize_schema(clock) == latest_version()
    with Database(db_path) as db:
        assert db.initialize_schema(clock) == latest_version()
    raw = _raw(db_path)
    try:
        rows = raw.execute("SELECT version, name, applied_at FROM schema_version").fetchall()
    finally:
        raw.close()
    assert rows == [(m.version, m.name, to_utc_text(f.T0)) for m in MIGRATIONS]


def test_foreign_keys_are_active(db: Database) -> None:
    with pytest.raises(IntegrityError):
        with db.transaction() as uow:
            uow.contacts.add(f.contact(company_id="missing-company"))


def test_newer_database_version_is_rejected(db_path: Path, clock: FrozenClock) -> None:
    with Database(db_path) as db:
        db.initialize_schema(clock)
    raw = _raw(db_path)
    try:
        raw.execute("INSERT INTO schema_version VALUES (99, 'future', '2030-01-01T00:00:00.000000+00:00')")
    finally:
        raw.close()
    with Database(db_path) as db, pytest.raises(SchemaVersionError):
        db.initialize_schema(clock)


# ---- Migration runner ---------------------------------------------------------


NEXT = len(MIGRATIONS) + 1


def _create(table: str) -> Migration:
    return Migration(NEXT, f"create_{table}", lambda c: c.execute(f"CREATE TABLE {table} (x INTEGER) STRICT"))


def test_pending_migration_applies_on_existing_database(clock: FrozenClock) -> None:
    connection = sqlite3.connect(MEMORY, isolation_level=None)
    try:
        assert apply_migrations(connection, clock) == latest_version()
        assert apply_migrations(connection, clock, (*MIGRATIONS, _create("extra"))) == NEXT
        assert apply_migrations(connection, clock, (*MIGRATIONS, _create("extra"))) == NEXT
        assert current_version(connection) == NEXT
    finally:
        connection.close()


def test_failing_migration_rolls_back_completely(clock: FrozenClock) -> None:
    def broken(connection: sqlite3.Connection) -> None:
        connection.execute("CREATE TABLE half_done (x INTEGER)")
        connection.execute("THIS IS NOT SQL")

    connection = sqlite3.connect(MEMORY, isolation_level=None)
    try:
        apply_migrations(connection, clock)
        with pytest.raises(sqlite3.OperationalError):
            apply_migrations(connection, clock, (*MIGRATIONS, Migration(NEXT, "broken", broken)))
        assert current_version(connection) == latest_version()
        leftover = connection.execute("SELECT name FROM sqlite_master WHERE name = 'half_done'").fetchone()
        assert leftover is None
        assert not connection.in_transaction
    finally:
        connection.close()


@pytest.mark.parametrize(
    "versions",
    [(2,), (1, 3), (2, 1), (1, 1)],
)
def test_migration_order_is_validated(versions: tuple[int, ...]) -> None:
    migrations = tuple(Migration(v, f"m{v}", lambda c: None) for v in versions)
    with pytest.raises(SchemaVersionError):
        validate_migration_order(migrations)


def test_migration_errors_are_translated_by_database(db_path: Path, clock: FrozenClock) -> None:
    raw = _raw(db_path)
    try:
        # A conflicting pre-existing object makes the v1 migration fail.
        raw.execute("CREATE TABLE companies (x INTEGER)")
    finally:
        raw.close()
    with Database(db_path) as db:
        with pytest.raises(PersistenceError):
            db.initialize_schema(clock)
        assert db.schema_version() == 0


# ---- Lifecycle ----------------------------------------------------------------


def test_data_survives_reopen(db_path: Path, clock: FrozenClock) -> None:
    with Database(db_path) as db:
        db.initialize_schema(clock)
        with db.transaction() as uow:
            uow.companies.add(f.company())
    with Database(db_path) as db, db.transaction() as uow:
        assert uow.companies.get(f.COMPANY_ID) == f.company()


def test_memory_databases_are_independent(clock: FrozenClock) -> None:
    with Database(MEMORY) as first, Database(MEMORY) as second:
        first.initialize_schema(clock)
        second.initialize_schema(clock)
        with first.transaction() as uow:
            uow.companies.add(f.company())
        with second.transaction() as uow:
            assert uow.companies.get(f.COMPANY_ID) is None


def test_lifecycle_errors(clock: FrozenClock) -> None:
    db = Database(MEMORY)
    assert not db.is_open
    with pytest.raises(PersistenceError, match="not connected"):
        db.initialize_schema(clock)
    with pytest.raises(PersistenceError, match="not connected"), db.transaction():
        pass
    db.connect()
    try:
        with pytest.raises(PersistenceError, match="already connected"):
            db.connect()
    finally:
        db.close()
    assert not db.is_open
    db.close()  # closing twice is harmless
    with pytest.raises(ValueError):
        Database(MEMORY, busy_timeout_ms=-1)


def test_raw_sqlite_errors_do_not_leak(clock: FrozenClock) -> None:
    with Database(MEMORY) as db:  # schema deliberately not initialized
        with pytest.raises(PersistenceError) as info, db.transaction() as uow:
            uow.leads.get("lead-1")
        assert not isinstance(info.value, sqlite3.Error)
