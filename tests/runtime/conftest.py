from collections.abc import Iterator
from pathlib import Path

import pytest

from app.persistence import Database, FrozenClock
from tests.inbound.builders import NOW
from tests.inbound.conftest import seed_knowledge


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "runtime.sqlite3"
    with Database(path) as database:
        database.initialize_schema(FrozenClock(NOW))
        seed_knowledge(database)
    return path


@pytest.fixture
def db(db_path: Path) -> Iterator[Database]:
    with Database(db_path) as database:
        yield database
