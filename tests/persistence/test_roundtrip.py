"""Every repository reconstructs the exact Stage 1 model it stored."""

from app.core.enums import (
    EmailDirection,
    OutboundDecision,
    OutboundStatus,
    RefKind,
)
from app.core.models import EntityRef
from app.persistence import Database
from app.persistence.serialization import model_to_json
from pydantic import BaseModel
from tests.persistence import factories as f


def assert_same(loaded: BaseModel | None, original: BaseModel) -> None:
    assert loaded is not None
    assert type(loaded) is type(original)
    assert loaded == original
    assert model_to_json(loaded) == model_to_json(original)


def test_company_contact_campaign_lead_roundtrip(seeded: Database) -> None:
    with seeded.transaction() as uow:
        assert_same(uow.companies.get(f.COMPANY_ID), f.company())
        assert_same(uow.contacts.get(f.CONTACT_ID), f.contact())
        assert_same(uow.campaigns.get(f.CAMPAIGN_ID), f.campaign())
        assert_same(uow.leads.get(f.LEAD_ID), f.lead())


def test_thread_and_messages_roundtrip(seeded: Database) -> None:
    inbound = f.inbound_message()
    outbound = f.inbound_message(
        message_id="msg-2",
        rfc_message_id="<msg-2@ourco.example>",
        direction=EmailDirection.OUTBOUND,
        from_address="outreach@ourco.example",
        received_at=None,
        sent_at=f.T0,
    )
    with seeded.transaction() as uow:
        uow.messages.add(inbound)
        uow.messages.add(outbound)
    with seeded.transaction() as uow:
        assert_same(uow.threads.get(f.THREAD_ID), f.thread())
        assert_same(uow.messages.get("msg-1"), inbound)
        assert_same(uow.messages.get_by_rfc_message_id("<msg-2@ourco.example>"), outbound)


def test_outbound_message_roundtrip_including_sent_state(seeded: Database) -> None:
    sent = f.outbound_message(
        outbound_id="out-2",
        idempotency_key="camp-1:lead-1:1",
        status=OutboundStatus.SENT,
        decision=OutboundDecision.SEND,
        decision_reasons=("eligible",),
        send_permit_id="permit-1",
        approved_at=f.T0,
        sending_at=f.T0,
        sent_at=f.T0,
        provider_message_id="prov-1",
        rfc_message_id="<out-2@ourco.example>",
    )
    with seeded.transaction() as uow:
        uow.outbound.add(sent)
    with seeded.transaction() as uow:
        assert_same(uow.outbound.get(f.OUTBOUND_ID), f.outbound_message())
        assert_same(uow.outbound.get_by_idempotency_key("camp-1:lead-1:1"), sent)


def test_follow_up_plan_roundtrip(seeded: Database) -> None:
    plan = f.follow_up_plan()
    with seeded.transaction() as uow:
        uow.follow_ups.add(plan)
    with seeded.transaction() as uow:
        assert_same(uow.follow_ups.get("plan-1"), plan)


def test_dnc_roundtrip(db: Database) -> None:
    entry = f.dnc_entry(expires_at=f.T0.replace(year=2027))
    with db.transaction() as uow:
        uow.dnc.add(entry)
    with db.transaction() as uow:
        assert_same(uow.dnc.get("dnc-1"), entry)


def test_escalation_roundtrip(seeded: Database) -> None:
    escalation = f.escalation()
    with seeded.transaction() as uow:
        uow.escalations.add(escalation)
    with seeded.transaction() as uow:
        assert_same(uow.escalations.get("esc-1"), escalation)


def test_operator_command_and_response_roundtrip(seeded: Database) -> None:
    response = f.operator_response()
    with seeded.transaction() as uow:
        uow.operator_responses.add(response)
    with seeded.transaction() as uow:
        assert_same(uow.operator_commands.get(f.COMMAND_ID), f.operator_command())
        assert_same(uow.operator_commands.get_by_telegram_update_id(1001), f.operator_command())
        [loaded] = uow.operator_responses.list_for_command(f.COMMAND_ID)
        assert_same(loaded, response)


def test_audit_roundtrip(db: Database) -> None:
    event = f.audit_event()
    with db.transaction() as uow:
        uow.audit.append(event)
    with db.transaction() as uow:
        assert_same(uow.audit.get("evt-1"), event)
        [by_subject] = uow.audit.list_for_subject(EntityRef(kind=RefKind.LEAD, id=f.LEAD_ID))
        assert_same(by_subject, event)


def test_provenance_roundtrip(db: Database) -> None:
    record = f.provenance_record()
    with db.transaction() as uow:
        uow.provenance.append(record)
    with db.transaction() as uow:
        [loaded] = uow.provenance.list_for_artifact(EntityRef(kind=RefKind.MESSAGE_DRAFT, id="draft-1"))
        assert_same(loaded, record)


def test_knowledge_source_meta_roundtrip(db: Database) -> None:
    source = f.knowledge_source()
    with db.transaction() as uow:
        uow.knowledge_sources.add(source)
    with db.transaction() as uow:
        assert_same(uow.knowledge_sources.get("src-pricing", 1), source)
