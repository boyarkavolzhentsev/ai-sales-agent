"""Minimal deterministic schema migrations.

Each migration runs in its own IMMEDIATE transaction together with the insert of its
``schema_version`` row, so a migration is either fully applied or not at all. The
current version is re-read inside that transaction, so two processes initializing the
same database cannot apply a migration twice.
"""

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from app.persistence.clock import Clock
from app.persistence.errors import SchemaVersionError
from app.persistence.schema import (
    V1_INITIAL_SCHEMA,
    V2_ADD_OUTBOUND_SENDING_AT,
    V2_QUOTA_SCHEMA,
    V3_KNOWLEDGE_INDEX_SCHEMA,
    V4_OPTIONAL_COMPANY_SCHEMA,
    V5_DISPATCH_ATTEMPTS_SCHEMA,
    V6_CONVERSATIONS_SCHEMA,
    V7_CAMPAIGN_EXECUTION_SCHEMA,
    V8_SALES_PIPELINE_SCHEMA,
    V9_COMMERCIAL_SCHEMA,
    V10_EMAIL_PROVIDER_SYNC_SCHEMA,
    V11_OPERATOR_CHANNEL_SYNC_SCHEMA,
    V12_AI_ENRICHMENT_JOBS_SCHEMA,
    V13_KNOWLEDGE_EMBEDDINGS_SCHEMA,
)
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
    # Table rebuilds need foreign-key enforcement off (it cannot be toggled inside a
    # transaction). The runner then verifies PRAGMA foreign_key_check before committing
    # and restores the connection's previous setting afterwards.
    foreign_keys_off: bool = False


def v1_initial_schema(connection: sqlite3.Connection) -> None:
    # Statements run one by one: executescript() would commit the open transaction.
    for statement in V1_INITIAL_SCHEMA:
        connection.execute(statement)


def v2_quota_reservations(connection: sqlite3.Connection) -> None:
    connection.execute(V2_ADD_OUTBOUND_SENDING_AT)
    # Backfill the new projected column from each stored model's own JSON. Parsed
    # directly (not via the current model class) so this migration stays fixed in time.
    rows = connection.execute(
        "SELECT outbound_id, json_extract(data, '$.sending_at') FROM outbound_messages "
        "WHERE json_extract(data, '$.sending_at') IS NOT NULL"
    ).fetchall()
    for outbound_id, sending_at in rows:
        connection.execute(
            "UPDATE outbound_messages SET sending_at = ? WHERE outbound_id = ?",
            (to_utc_text(datetime.fromisoformat(sending_at)), outbound_id),
        )
    for statement in V2_QUOTA_SCHEMA:
        connection.execute(statement)


def v3_knowledge_index(connection: sqlite3.Connection) -> None:
    for statement in V3_KNOWLEDGE_INDEX_SCHEMA:
        connection.execute(statement)


def v4_optional_company(connection: sqlite3.Connection) -> None:
    for statement in V4_OPTIONAL_COMPANY_SCHEMA:
        connection.execute(statement)


def v5_dispatch_attempts(connection: sqlite3.Connection) -> None:
    for statement in V5_DISPATCH_ATTEMPTS_SCHEMA:
        connection.execute(statement)


def v6_conversations(connection: sqlite3.Connection) -> None:
    for statement in V6_CONVERSATIONS_SCHEMA:
        connection.execute(statement)


def v7_campaign_execution(connection: sqlite3.Connection) -> None:
    for statement in V7_CAMPAIGN_EXECUTION_SCHEMA:
        connection.execute(statement)


def v8_sales_pipeline(connection: sqlite3.Connection) -> None:
    for statement in V8_SALES_PIPELINE_SCHEMA:
        connection.execute(statement)


def v9_commercial(connection: sqlite3.Connection) -> None:
    for statement in V9_COMMERCIAL_SCHEMA:
        connection.execute(statement)


def v10_email_provider_sync(connection: sqlite3.Connection) -> None:
    for statement in V10_EMAIL_PROVIDER_SYNC_SCHEMA:
        connection.execute(statement)


def v11_operator_channel_sync(connection: sqlite3.Connection) -> None:
    for statement in V11_OPERATOR_CHANNEL_SYNC_SCHEMA:
        connection.execute(statement)


def v12_ai_enrichment_jobs(connection: sqlite3.Connection) -> None:
    for statement in V12_AI_ENRICHMENT_JOBS_SCHEMA:
        connection.execute(statement)


def v13_knowledge_embeddings(connection: sqlite3.Connection) -> None:
    for statement in V13_KNOWLEDGE_EMBEDDINGS_SCHEMA:
        connection.execute(statement)


MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "initial_schema", v1_initial_schema),
    Migration(2, "quota_reservations", v2_quota_reservations),
    Migration(3, "knowledge_index", v3_knowledge_index),
    Migration(4, "optional_company", v4_optional_company, foreign_keys_off=True),
    Migration(5, "dispatch_attempts", v5_dispatch_attempts),
    Migration(6, "conversations", v6_conversations),
    Migration(7, "campaign_execution", v7_campaign_execution),
    Migration(8, "sales_pipeline", v8_sales_pipeline),
    Migration(9, "commercial_decisioning", v9_commercial),
    Migration(10, "email_provider_sync", v10_email_provider_sync),
    Migration(11, "operator_channel_sync", v11_operator_channel_sync),
    Migration(12, "ai_enrichment_jobs", v12_ai_enrichment_jobs),
    Migration(13, "knowledge_embeddings", v13_knowledge_embeddings),
)


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
        if migration.foreign_keys_off and current_version(connection) < migration.version:
            _run_with_foreign_keys_off(connection, migration, clock)
        else:
            _run_in_transaction(connection, lambda m=migration: _apply_if_pending(connection, m, clock))
    return current_version(connection)


def _run_with_foreign_keys_off(connection: sqlite3.Connection, migration: Migration, clock: Clock) -> None:
    enabled = connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        _run_in_transaction(connection, lambda: _apply_if_pending(connection, migration, clock))
    finally:
        connection.execute("PRAGMA foreign_keys = ON" if enabled else "PRAGMA foreign_keys = OFF")


def _apply_if_pending(connection: sqlite3.Connection, migration: Migration, clock: Clock) -> None:
    if current_version(connection) >= migration.version:
        return
    migration.apply(connection)
    if migration.foreign_keys_off:
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise sqlite3.IntegrityError(
                f"migration {migration.version} left foreign key violations: {[tuple(v) for v in violations[:5]]}"
            )
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
