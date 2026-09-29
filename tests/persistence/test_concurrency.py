"""Optimistic concurrency for every mutable aggregate: update(entity, expected_version)."""

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import BaseModel

from app.core.enums import (
    CampaignStatus,
    EscalationStatus,
    LeadStage,
    OutboundDecision,
    OutboundStatus,
)
from app.persistence import ConcurrencyError, Database, FrozenClock, NotFoundError
from tests.persistence import factories as f

LATER = f.T0 + timedelta(hours=1)


@dataclass(frozen=True)
class Aggregate:
    repo: str
    id_field: str
    key: str
    original: Callable[[], BaseModel]
    # Two different edits, so competing writers produce distinguishable states.
    edit_a: Callable[[int], BaseModel]
    edit_b: Callable[[int], BaseModel]
    needs_add: bool = False


AGGREGATES = {
    "company": Aggregate(
        "companies", "company_id", f.COMPANY_ID, f.company,
        lambda v: f.company(name="Renamed Ltd", version=v, updated_at=LATER),
        lambda v: f.company(industry="Logistics", version=v, updated_at=LATER),
    ),
    "contact": Aggregate(
        "contacts", "contact_id", f.CONTACT_ID, f.contact,
        lambda v: f.contact(name="Partner Desk", version=v, updated_at=LATER),
        lambda v: f.contact(role_title="Head of Partnerships", version=v, updated_at=LATER),
    ),
    "thread": Aggregate(
        "threads", "thread_id", f.THREAD_ID, f.thread,
        lambda v: f.thread(message_ids=("msg-1", "msg-2"), version=v),
        lambda v: f.thread(last_inbound_at=LATER, version=v),
    ),
    "campaign": Aggregate(
        "campaigns", "campaign_id", f.CAMPAIGN_ID, f.campaign,
        lambda v: f.campaign(name="Renamed campaign", version=v, updated_at=LATER),
        lambda v: f.campaign(max_follow_ups=1, config_version=2, version=v, updated_at=LATER),
    ),
    "outbound": Aggregate(
        "outbound", "outbound_id", f.OUTBOUND_ID, f.outbound_message,
        lambda v: f.outbound_message(
            status=OutboundStatus.HELD, decision=OutboundDecision.HOLD, hold_reason="daily limit", version=v
        ),
        lambda v: f.outbound_message(status=OutboundStatus.CANCELLED, version=v),
    ),
    "escalation": Aggregate(
        "escalations", "escalation_id", "esc-1", f.escalation,
        lambda v: f.escalation(status=EscalationStatus.ACKNOWLEDGED, version=v),
        lambda v: f.escalation(status=EscalationStatus.EXPIRED, version=v),
        needs_add=True,
    ),
    "lead": Aggregate(
        "leads", "lead_id", f.LEAD_ID, f.lead,
        lambda v: f.lead(stage=LeadStage.CONTACTED, version=v, updated_at=LATER),
        lambda v: f.lead(stage=LeadStage.ENGAGED, version=v, updated_at=LATER),
    ),
    "follow_up_plan": Aggregate(
        "follow_ups", "plan_id", "plan-1", f.follow_up_plan,
        lambda v: f.follow_up_plan(steps_sent=1, version=v, updated_at=LATER),
        lambda v: f.follow_up_plan(next_due_at=LATER + timedelta(days=9), version=v, updated_at=LATER),
        needs_add=True,
    ),
}


@pytest.fixture(params=list(AGGREGATES), ids=list(AGGREGATES))
def case(request: pytest.FixtureRequest, seeded: Database) -> tuple[Database, Aggregate]:
    aggregate = AGGREGATES[request.param]
    if aggregate.needs_add:
        with seeded.transaction() as uow:
            getattr(uow, aggregate.repo).add(aggregate.original())
    return seeded, aggregate


def _stored(db: Database, aggregate: Aggregate) -> BaseModel | None:
    with db.transaction() as uow:
        stored: BaseModel | None = getattr(uow, aggregate.repo).get(aggregate.key)
    return stored


def _update(db: Database, aggregate: Aggregate, entity: BaseModel, expected_version: int) -> None:
    with db.transaction() as uow:
        getattr(uow, aggregate.repo).update(entity, expected_version)


# A. initial roundtrip preserves version
def test_initial_roundtrip_preserves_version(case: tuple[Database, Aggregate]) -> None:
    db, aggregate = case
    stored = _stored(db, aggregate)
    assert stored == aggregate.original()
    assert getattr(stored, "version") == 1


# B + C. correct expected_version succeeds and version increments by exactly 1
def test_update_with_expected_version_increments_by_one(case: tuple[Database, Aggregate]) -> None:
    db, aggregate = case
    _update(db, aggregate, aggregate.edit_a(2), expected_version=1)
    assert _stored(db, aggregate) == aggregate.edit_a(2)
    _update(db, aggregate, aggregate.edit_b(3), expected_version=2)
    stored = _stored(db, aggregate)
    assert stored == aggregate.edit_b(3)
    assert getattr(stored, "version") == 3


# D + F. stale expected_version raises and leaves the stored entity untouched
def test_stale_expected_version_is_rejected_and_state_kept(case: tuple[Database, Aggregate]) -> None:
    db, aggregate = case
    _update(db, aggregate, aggregate.edit_a(2), expected_version=1)  # writer A wins
    with pytest.raises(ConcurrencyError):
        _update(db, aggregate, aggregate.edit_b(2), expected_version=1)  # writer B read v1
    assert _stored(db, aggregate) == aggregate.edit_a(2)


# E. the new entity must carry exactly expected_version + 1
@pytest.mark.parametrize(("new_version", "expected_version"), [(1, 1), (3, 1), (2, 2), (1, 0)])
def test_wrong_new_version_is_rejected(
    case: tuple[Database, Aggregate], new_version: int, expected_version: int
) -> None:
    db, aggregate = case
    with pytest.raises(ValueError, match="expected_version"):
        _update(db, aggregate, aggregate.edit_a(new_version), expected_version)
    assert _stored(db, aggregate) == aggregate.original()


def test_update_of_missing_record_is_not_found(case: tuple[Database, Aggregate]) -> None:
    db, aggregate = case
    with db.transaction() as uow:
        repo = getattr(uow, aggregate.repo)
        missing = aggregate.edit_a(2).model_copy(update={aggregate.id_field: "does-not-exist"})
        with pytest.raises(NotFoundError):
            repo.update(missing, 1)


# ---- Campaign: version and config_version are different concepts -------------------


def test_campaign_version_moves_while_config_version_stays(seeded: Database) -> None:
    active = f.campaign(status=CampaignStatus.ACTIVE, activated_by="operator-1", version=2, updated_at=LATER)
    paused = f.campaign(status=CampaignStatus.PAUSED, activated_by="operator-1", version=3, updated_at=LATER)
    with seeded.transaction() as uow:
        uow.campaigns.update(active, expected_version=1)
        uow.campaigns.update(paused, expected_version=2)
    with seeded.transaction() as uow:
        stored = uow.campaigns.get(f.CAMPAIGN_ID)
    assert stored == paused
    assert stored is not None
    assert (stored.version, stored.config_version) == (3, 1)


def test_campaign_same_status_and_config_does_not_bypass_version(seeded: Database) -> None:
    # Two writers both read version 1 with status DRAFT / config_version 1. Under the old
    # status/config compare-and-set the second write would have been accepted.
    first = f.campaign(name="Writer A", version=2, updated_at=LATER)
    second = f.campaign(name="Writer B", version=2, updated_at=LATER)
    with seeded.transaction() as uow:
        uow.campaigns.update(first, expected_version=1)
    with pytest.raises(ConcurrencyError), seeded.transaction() as uow:
        uow.campaigns.update(second, expected_version=1)
    with seeded.transaction() as uow:
        assert uow.campaigns.get(f.CAMPAIGN_ID) == first


# ---- Projected SQL version always matches the model version ------------------------


def test_projected_version_column_matches_model(db_path: Path, clock: FrozenClock) -> None:
    with Database(db_path) as db:
        db.initialize_schema(clock)
        with db.transaction() as uow:
            uow.companies.add(f.company())
            uow.companies.update(f.company(name="Renamed Ltd", version=2), expected_version=1)
    raw = sqlite3.connect(db_path, isolation_level=None)
    try:
        column, embedded = raw.execute(
            "SELECT version, json_extract(data, '$.version') FROM companies"
        ).fetchone()
        assert column == embedded == 2
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            raw.execute("UPDATE companies SET version = 5")
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            raw.execute("UPDATE companies SET version = 0, data = json_set(data, '$.version', 0)")
    finally:
        raw.close()
