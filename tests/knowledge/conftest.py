from collections.abc import Iterator
from pathlib import Path

import pytest

from app.knowledge import ingest_directory
from app.persistence import MEMORY, Database, FrozenClock
from tests.knowledge.sources import FIXTURE_ROOT, NOW


@pytest.fixture
def db() -> Iterator[Database]:
    with Database(MEMORY) as database:
        database.initialize_schema(FrozenClock(NOW))
        yield database


@pytest.fixture
def kb_root(tmp_path: Path) -> Path:
    root = tmp_path / "kb"
    root.mkdir()
    return root


@pytest.fixture
def fixture_db(db: Database) -> Database:
    """The fictional fixture knowledge base, ingested."""
    with db.transaction() as uow:
        ingest_directory(uow, FIXTURE_ROOT, now=NOW)
    return db


def ingest(db: Database, root: Path) -> None:
    with db.transaction() as uow:
        ingest_directory(uow, root, now=NOW)
