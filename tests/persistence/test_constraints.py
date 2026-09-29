from datetime import timedelta

import pytest

from app.core.enums import FollowUpCancelReason, FollowUpStatus
from app.persistence import AlreadyExistsError, Database, IntegrityError
from tests.persistence import factories as f


def test_duplicate_contact_email_rejected_after_normalization(seeded: Database) -> None:
    with pytest.raises(AlreadyExistsError), seeded.transaction() as uow:
        uow.contacts.add(f.contact(contact_id="contact-2", email="Partners@Prospect.EXAMPLE"))


def test_duplicate_company_domain_rejected(seeded: Database) -> None:
    with pytest.raises(AlreadyExistsError), seeded.transaction() as uow:
        uow.companies.add(f.company(company_id="company-2", domain="PROSPECT.example"))


def test_duplicate_primary_key_rejected(seeded: Database) -> None:
    with pytest.raises(AlreadyExistsError), seeded.transaction() as uow:
        uow.leads.add(f.lead())


def test_duplicate_rfc_message_id_rejected(seeded: Database) -> None:
    with seeded.transaction() as uow:
        uow.messages.add(f.inbound_message())
    with pytest.raises(AlreadyExistsError), seeded.transaction() as uow:
        uow.messages.add(f.inbound_message(message_id="msg-other"))
    with seeded.transaction() as uow:
        assert uow.messages.get("msg-other") is None
        assert uow.messages.exists_by_rfc_message_id("<msg-1@prospect.example>")
        assert not uow.messages.exists_by_rfc_message_id("<unknown@x.example>")


def test_duplicate_outbound_idempotency_key_rejected(seeded: Database) -> None:
    with pytest.raises(AlreadyExistsError), seeded.transaction() as uow:
        uow.outbound.add(f.outbound_message(outbound_id="out-other"))


def test_duplicate_idempotency_key_rejected(db: Database) -> None:
    with db.transaction() as uow:
        uow.idempotency.reserve("op:1", "test", f.T0)
    with pytest.raises(AlreadyExistsError), db.transaction() as uow:
        uow.idempotency.reserve("op:1", "test", f.T0)


def test_duplicate_telegram_update_id_rejected(seeded: Database) -> None:
    with pytest.raises(AlreadyExistsError), seeded.transaction() as uow:
        uow.operator_commands.add(f.operator_command(command_id="cmd-other"))


def test_duplicate_audit_event_rejected(db: Database) -> None:
    with db.transaction() as uow:
        uow.audit.append(f.audit_event())
    with pytest.raises(AlreadyExistsError), db.transaction() as uow:
        uow.audit.append(f.audit_event())


def test_duplicate_knowledge_source_version_rejected(db: Database) -> None:
    with db.transaction() as uow:
        uow.knowledge_sources.add(f.knowledge_source())
    with pytest.raises(AlreadyExistsError), db.transaction() as uow:
        uow.knowledge_sources.add(f.knowledge_source())
    with db.transaction() as uow:
        uow.knowledge_sources.add(f.knowledge_source(version=2))


@pytest.mark.parametrize(
    "add",
    [
        lambda uow: uow.leads.add(f.lead(lead_id="lead-x", contact_id="missing")),
        lambda uow: uow.leads.add(f.lead(lead_id="lead-x", campaign_id="missing")),
        lambda uow: uow.messages.add(f.inbound_message(thread_id="missing")),
        lambda uow: uow.follow_ups.add(f.follow_up_plan(anchor_outbound_id="missing")),
        lambda uow: uow.escalations.add(f.escalation(lead_id="missing")),
        lambda uow: uow.operator_responses.add(f.operator_response(command_id="missing")),
    ],
)
def test_missing_foreign_keys_rejected(seeded: Database, add: object) -> None:
    with pytest.raises(IntegrityError), seeded.transaction() as uow:
        add(uow)  # type: ignore[operator]


def test_only_one_open_follow_up_plan_per_lead(seeded: Database) -> None:
    with seeded.transaction() as uow:
        uow.follow_ups.add(f.follow_up_plan())
    with pytest.raises(AlreadyExistsError), seeded.transaction() as uow:
        uow.follow_ups.add(f.follow_up_plan(plan_id="plan-2", status=FollowUpStatus.PAUSED))
    # A closed plan does not block a new open one.
    with seeded.transaction() as uow:
        uow.follow_ups.add(
            f.follow_up_plan(
                plan_id="plan-old",
                status=FollowUpStatus.CANCELLED,
                cancel_reason=FollowUpCancelReason.OPERATOR,
                next_due_at=None,
                created_at=f.T0 - timedelta(days=30),
                updated_at=f.T0 - timedelta(days=30),
            )
        )
