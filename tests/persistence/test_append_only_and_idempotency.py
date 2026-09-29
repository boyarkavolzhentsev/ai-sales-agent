import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.persistence import (
    Database,
    DuplicateIdempotencyKeyError,
    FrozenClock,
    IdempotencyRecord,
)
from app.persistence.repositories.sqlite import (
    SqliteAuditRepository,
    SqliteDoNotContactRepository,
    SqliteIdempotencyRepository,
    SqliteKnowledgeSourceMetaRepository,
    SqliteProvenanceRepository,
)
from tests.persistence import factories as f

MUTATORS = ("update", "delete", "remove", "replace", "upsert", "save", "set")


# ---- Append-only: API surface -------------------------------------------------


@pytest.mark.parametrize(
    "repository",
    [
        SqliteAuditRepository,
        SqliteProvenanceRepository,
        SqliteDoNotContactRepository,
        SqliteKnowledgeSourceMetaRepository,
        SqliteIdempotencyRepository,
    ],
)
def test_append_only_repositories_expose_no_mutation(repository: type) -> None:
    public = {name for name in dir(repository) if not name.startswith("_")}
    assert not {name for name in public if name.startswith(MUTATORS)}


# ---- Append-only: database enforcement -----------------------------------------


@pytest.fixture
def populated_file_db(db_path: Path, clock: FrozenClock) -> Path:
    with Database(db_path) as db:
        db.initialize_schema(clock)
        with db.transaction() as uow:
            uow.audit.append(f.audit_event())
            uow.provenance.append(f.provenance_record())
            uow.dnc.add(f.dnc_entry())
    return db_path


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE audit_events SET event_type = 'TAMPERED'",
        "DELETE FROM audit_events",
        "UPDATE audit_event_subjects SET subject_id = 'other'",
        "DELETE FROM audit_event_subjects",
        "UPDATE provenance_records SET artifact_id = 'other'",
        "DELETE FROM provenance_records",
        "UPDATE do_not_contact SET value = 'other@x.example'",
        "DELETE FROM do_not_contact",
    ],
)
def test_database_rejects_mutation_of_append_only_tables(
    populated_file_db: Path, statement: str
) -> None:
    raw = sqlite3.connect(populated_file_db, isolation_level=None)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            raw.execute(statement)
        table = statement.split()[2] if statement.startswith("DELETE") else statement.split()[1]
        assert raw.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] >= 1
    finally:
        raw.close()


def test_audit_append_is_queryable_by_correlation(db: Database) -> None:
    with db.transaction() as uow:
        uow.audit.append(f.audit_event(event_id="evt-2", occurred_at=f.T0.replace(hour=13)))
        uow.audit.append(f.audit_event())
        uow.audit.append(f.audit_event(event_id="evt-3", correlation_id="corr-other"))
    with db.transaction() as uow:
        assert [e.event_id for e in uow.audit.list_by_correlation_id("corr-1")] == ["evt-1", "evt-2"]


# ---- Idempotency ----------------------------------------------------------------


def test_first_reserve_succeeds(db: Database) -> None:
    with db.transaction() as uow:
        record = uow.idempotency.reserve("send:out-1", "outbound.send", f.T0)
        assert record == IdempotencyRecord(key="send:out-1", operation="outbound.send", created_at=f.T0)
    with db.transaction() as uow:
        assert uow.idempotency.exists("send:out-1")
        assert uow.idempotency.get("send:out-1") == record
        assert not uow.idempotency.exists("send:other")
        assert uow.idempotency.get("send:other") is None


def test_second_reserve_is_a_deterministic_duplicate(db: Database) -> None:
    with db.transaction() as uow:
        original = uow.idempotency.reserve("send:out-1", "outbound.send", f.T0)
    for operation in ("outbound.send", "something.else"):
        with pytest.raises(DuplicateIdempotencyKeyError) as info, db.transaction() as uow:
            uow.idempotency.reserve("send:out-1", operation, f.T0.replace(hour=18))
        assert info.value.existing == original
    with db.transaction() as uow:
        assert uow.idempotency.get("send:out-1") == original


def test_duplicate_reserve_inside_a_transaction_keeps_other_writes(db: Database) -> None:
    with db.transaction() as uow:
        uow.idempotency.reserve("k", "op", f.T0)
    with db.transaction() as uow:
        uow.companies.add(f.company())
        with pytest.raises(DuplicateIdempotencyKeyError):
            uow.idempotency.reserve("k", "op", f.T0)
    with db.transaction() as uow:
        assert uow.companies.get(f.COMPANY_ID) is not None


def test_rolled_back_reservation_does_not_exist(db: Database) -> None:
    with pytest.raises(RuntimeError), db.transaction() as uow:
        uow.idempotency.reserve("k", "op", f.T0)
        raise RuntimeError
    with db.transaction() as uow:
        assert not uow.idempotency.exists("k")


def test_reserve_validates_inputs(db: Database) -> None:
    with db.transaction() as uow:
        with pytest.raises(ValidationError):
            uow.idempotency.reserve("  ", "op", f.T0)
        with pytest.raises(ValidationError):
            uow.idempotency.reserve("k", "op", f.T0.replace(tzinfo=None))
        assert not uow.idempotency.exists("  ")
