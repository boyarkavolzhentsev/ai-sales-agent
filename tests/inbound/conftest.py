from collections.abc import Iterator
from pathlib import Path

import pytest

from app.knowledge import ingest_directory
from app.persistence import MEMORY, Database, FrozenClock
from tests.inbound.builders import NOW
from tests.knowledge.sources import FIXTURE_ROOT


def seed_knowledge(db: Database) -> None:
    with db.transaction() as uow:
        ingest_directory(uow, FIXTURE_ROOT, now=NOW)


@pytest.fixture
def db() -> Iterator[Database]:
    with Database(MEMORY) as database:
        database.initialize_schema(FrozenClock(NOW))
        seed_knowledge(database)
        yield database


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "inbound.sqlite3"
    with Database(path) as database:
        database.initialize_schema(FrozenClock(NOW))
        seed_knowledge(database)
    return path
