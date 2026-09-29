"""J. Migration v2: quota_reservations table and outbound_messages.sending_at backfill."""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.core.models import OutboundMessage
from app.persistence import Database, FrozenClock
from app.persistence.migrations import MIGRATIONS, apply_migrations, current_version, latest_version
from app.persistence.serialization import model_to_json, optional_utc_text, to_utc_text
from tests.policy import builders as b

KYIV_SENDING = datetime(2026, 1, 1, 14, 5, tzinfo=UTC).astimezone()  # any aware offset


def _v1_database(path: Path, clock: FrozenClock, *messages: OutboundMessage) -> None:
    """Build a database at schema v1 and insert outbound rows exactly as v1 code did."""
    connection = sqlite3.connect(path, isolation_level=None)  # raw connection: FKs off
    try:
        assert apply_migrations(connection, clock, MIGRATIONS[:1]) == 1
        for m in messages:
            connection.execute(
                "INSERT INTO outbound_messages (outbound_id, idempotency_key, kind, status, lead_id, "
                "contact_id, campaign_id, thread_id, created_at, sent_at, version, data) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    m.outbound_id, m.idempotency_key, m.kind.value, m.status.value, m.lead_id,
                    m.contact_id, m.campaign_id, m.thread_id, to_utc_text(m.created_at),
                    optional_utc_text(m.sent_at), m.version, model_to_json(m),
                ),
            )
    finally:
        connection.close()


def _columns(path: Path, table: str) -> set[str]:
    connection = sqlite3.connect(path)
    try:
        return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    finally:
        connection.close()


def test_v1_database_migrates_to_v2_with_backfill(db_path: Path, clock: FrozenClock) -> None:
    sent = b.sent_message("sent", sending_at=KYIV_SENDING)
    drafted = b.approved_message("approved")
    _v1_database(db_path, clock, sent, drafted)
    assert "sending_at" not in _columns(db_path, "outbound_messages")

    clock.advance(timedelta(days=1))
    raw = sqlite3.connect(db_path, isolation_level=None)
    try:
        assert apply_migrations(raw, clock, MIGRATIONS[:2]) == 2
    finally:
        raw.close()

    connection = sqlite3.connect(db_path)
    try:
        rows = dict(connection.execute("SELECT outbound_id, sending_at FROM outbound_messages"))
        versions = connection.execute("SELECT version, name, applied_at FROM schema_version ORDER BY version").fetchall()
    finally:
        connection.close()
    assert rows == {"sent": to_utc_text(KYIV_SENDING), "approved": None}
    assert versions == [
        (1, "initial_schema", to_utc_text(b.T0)),
        (2, "quota_reservations", to_utc_text(b.T0 + timedelta(days=1))),
    ]
    assert {"reservation_id", "outbound_id", "policy_date", "state", "version", "data"} <= _columns(
        db_path, "quota_reservations"
    )


def test_fresh_database_reaches_latest(db_path: Path, clock: FrozenClock) -> None:
    with Database(db_path) as db:
        assert db.schema_version() == 0
        assert db.initialize_schema(clock) == latest_version() == len(MIGRATIONS)
    assert "sending_at" in _columns(db_path, "outbound_messages")


def test_initialize_is_idempotent_across_versions(db_path: Path, clock: FrozenClock) -> None:
    _v1_database(db_path, clock)
    with Database(db_path) as db:
        assert db.initialize_schema(clock) == latest_version()
        assert db.initialize_schema(clock) == latest_version()
    with Database(db_path) as db:
        assert db.initialize_schema(clock) == latest_version()
    connection = sqlite3.connect(db_path, isolation_level=None)
    try:
        assert current_version(connection) == latest_version()
        assert connection.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == len(MIGRATIONS)
    finally:
        connection.close()
