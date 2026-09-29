from datetime import timedelta

import pytest

from app.core.enums import FollowUpCancelReason, FollowUpStatus, LeadStage, RefKind
from app.core.models import EntityRef, FollowUpPlan, Lead
from app.persistence import ConcurrencyError, Database, PersistenceError
from tests.persistence import factories as f

LATER = f.T0 + timedelta(hours=1)
LEAD_REF = EntityRef(kind=RefKind.LEAD, id=f.LEAD_ID)


class Boom(Exception):
    pass


def _contacted_lead() -> Lead:
    return f.lead(stage=LeadStage.CONTACTED, version=2, updated_at=LATER)


def _cancelled_plan() -> FollowUpPlan:
    return f.follow_up_plan(
        status=FollowUpStatus.CANCELLED,
        cancel_reason=FollowUpCancelReason.REPLY_RECEIVED,
        next_due_at=None,
        version=2,
        updated_at=LATER,
    )


@pytest.fixture
def with_plan(seeded: Database) -> Database:
    with seeded.transaction() as uow:
        uow.follow_ups.add(f.follow_up_plan())
    return seeded


def test_commit_persists_all_writes(with_plan: Database) -> None:
    with with_plan.transaction() as uow:
        uow.leads.update(_contacted_lead(), 1)
        uow.follow_ups.update(_cancelled_plan(), 1)
        uow.audit.append(f.audit_event())
    with with_plan.transaction() as uow:
        assert uow.leads.get(f.LEAD_ID) == _contacted_lead()
        assert uow.follow_ups.get("plan-1") == _cancelled_plan()
        assert len(uow.audit.list_for_subject(LEAD_REF)) == 1


def test_exception_rolls_back_every_write(with_plan: Database) -> None:
    with pytest.raises(Boom), with_plan.transaction() as uow:
        uow.leads.update(_contacted_lead(), 1)
        uow.follow_ups.update(_cancelled_plan(), 1)
        uow.audit.append(f.audit_event())
        raise Boom
    with with_plan.transaction() as uow:
        assert uow.leads.get(f.LEAD_ID) == f.lead()
        assert uow.follow_ups.get("plan-1") == f.follow_up_plan()
        assert uow.audit.list_for_subject(LEAD_REF) == []


def test_persistence_error_mid_transaction_rolls_back_earlier_writes(with_plan: Database) -> None:
    with pytest.raises(ConcurrencyError), with_plan.transaction() as uow:
        uow.audit.append(f.audit_event())
        uow.leads.update(_contacted_lead(), 1)
        stale = _cancelled_plan().model_copy(update={"version": 6})
        uow.follow_ups.update(stale, expected_version=5)  # stored version is 1
    with with_plan.transaction() as uow:
        assert uow.leads.get(f.LEAD_ID) == f.lead()
        assert uow.audit.get("evt-1") is None


def test_unit_of_work_is_unusable_after_the_transaction(seeded: Database) -> None:
    with seeded.transaction() as uow:
        pass
    with pytest.raises(PersistenceError, match="no longer active"):
        uow.leads.get(f.LEAD_ID)
    with pytest.raises(PersistenceError, match="no longer active"):
        uow.audit.append(f.audit_event())


def test_nested_transactions_are_rejected(db: Database) -> None:
    with db.transaction(), pytest.raises(PersistenceError, match="nested"), db.transaction():
        pass


def test_transaction_usable_again_after_rollback(db: Database) -> None:
    with pytest.raises(Boom), db.transaction():
        raise Boom
    with db.transaction() as uow:
        uow.companies.add(f.company())
    with db.transaction() as uow:
        assert uow.companies.get(f.COMPANY_ID) is not None
