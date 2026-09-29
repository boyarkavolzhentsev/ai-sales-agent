from collections.abc import Iterator
from pathlib import Path

import pytest

from app.persistence import MEMORY, Database, FrozenClock
from tests.persistence import factories as f
from tests.policy import builders as b


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(f.T0)


@pytest.fixture
def db(clock: FrozenClock) -> Iterator[Database]:
    with Database(MEMORY) as database:
        database.initialize_schema(clock)
        yield database


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "policy.sqlite3"


def seed(db: Database) -> None:
    """Parent rows for reservation tests: an ACTIVE campaign, its lead, and a reply thread."""
    with db.transaction() as uow:
        uow.companies.add(f.company())
        uow.contacts.add(f.contact())
        uow.campaigns.add(b.active_campaign())
        uow.leads.add(f.lead())
        uow.threads.add(f.thread(mailbox="support@ourco.example"))


@pytest.fixture
def seeded(db: Database) -> Database:
    seed(db)
    return db
