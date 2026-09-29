from collections.abc import Iterator
from pathlib import Path

import pytest

from app.persistence import MEMORY, Database, FrozenClock
from tests.persistence import factories as f


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(f.T0)


@pytest.fixture
def db(clock: FrozenClock) -> Iterator[Database]:
    """Fresh, initialized in-memory database per test."""
    with Database(MEMORY) as database:
        database.initialize_schema(clock)
        yield database


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Path to a per-test temporary database file (pytest removes tmp_path)."""
    return tmp_path / "test.sqlite3"


@pytest.fixture
def seeded(db: Database) -> Database:
    """Database containing the parent rows most aggregates reference."""
    with db.transaction() as uow:
        uow.companies.add(f.company())
        uow.contacts.add(f.contact())
        uow.campaigns.add(f.campaign())
        uow.leads.add(f.lead())
        uow.threads.add(f.thread())
        uow.outbound.add(f.outbound_message())
        uow.operator_commands.add(f.operator_command())
    return db
