"""H. Migration v3: knowledge chunks, facts, FTS5 index and immutability guards."""

import sqlite3
from pathlib import Path

import pytest

from app.knowledge import parse_source_text
from app.persistence import Database, FrozenClock
from app.persistence.migrations import MIGRATIONS, apply_migrations, current_version, latest_version
from app.persistence.serialization import model_to_json
from tests.knowledge.sources import NOW, markdown, meta

V3_TABLES = {"knowledge_chunks", "knowledge_facts", "knowledge_chunks_fts"}


def _tables(path: Path) -> set[str]:
    connection = sqlite3.connect(path)
    try:
        return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        connection.close()


def test_fresh_database_applies_v1_v2_v3(db_path: Path) -> None:
    with Database(db_path) as db:
        assert db.initialize_schema(FrozenClock(NOW)) == latest_version() == 3
    assert V3_TABLES <= _tables(db_path)
    connection = sqlite3.connect(db_path)
    try:
        names = [row[0] for row in connection.execute("SELECT name FROM schema_version ORDER BY version")]
    finally:
        connection.close()
    assert names == ["initial_schema", "quota_reservations", "knowledge_index"]


def test_v2_database_migrates_to_v3_and_protects_existing_knowledge(db_path: Path) -> None:
    raw = sqlite3.connect(db_path, isolation_level=None)
    try:
        assert apply_migrations(raw, FrozenClock(NOW), MIGRATIONS[:2]) == 2
        source = parse_source_text(markdown(meta()), extension=".md", label="faq/a.md").source
        raw.execute(
            "INSERT INTO knowledge_sources_meta (source_id, version, domain, approval_status, "
            "external_use, content_hash, data) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (source.source_id, source.version, source.domain.value, source.approval_status.value,
             source.external_use.value, source.content_hash, model_to_json(source)),
        )
        raw.execute("UPDATE knowledge_sources_meta SET domain = domain")  # still mutable at v2
    finally:
        raw.close()
    assert not V3_TABLES & _tables(db_path)

    with Database(db_path) as db:
        assert db.initialize_schema(FrozenClock(NOW)) == 3
        with db.transaction() as uow:
            assert uow.knowledge_sources.get("src-sample", 1) == source
    assert V3_TABLES <= _tables(db_path)
    raw = sqlite3.connect(db_path, isolation_level=None)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            raw.execute("UPDATE knowledge_sources_meta SET domain = domain")
    finally:
        raw.close()


def test_migration_is_idempotent(db_path: Path) -> None:
    with Database(db_path) as db:
        db.initialize_schema(FrozenClock(NOW))
        assert db.initialize_schema(FrozenClock(NOW)) == 3
    with Database(db_path) as db:
        assert db.initialize_schema(FrozenClock(NOW)) == 3
    raw = sqlite3.connect(db_path, isolation_level=None)
    try:
        assert current_version(raw) == 3
        assert raw.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == len(MIGRATIONS)
    finally:
        raw.close()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "knowledge.sqlite3"
