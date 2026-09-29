from collections.abc import Iterator
from pathlib import Path

import pytest

from app.persistence import MEMORY, Database, FrozenClock
from tests.inbound.builders import NOW
from tests.inbound.conftest import seed_knowledge


@pytest.fixture
def db() -> Iterator[Database]:
    with Database(MEMORY) as database:
        database.initialize_schema(FrozenClock(NOW))
        seed_knowledge(database)
        yield database


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "operator.sqlite3"
    with Database(path) as database:
        database.initialize_schema(FrozenClock(NOW))
        seed_knowledge(database)
    return path
