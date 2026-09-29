from datetime import timedelta

import pytest

from app.core.enums import (
    DNCScope,
    EmailDirection,
    EscalationStatus,
    FollowUpStatus,
    KnowledgeApprovalStatus,
    KnowledgeDomain,
    OperatorResponseStatus,
    OutboundStatus,
    RefKind,
)
from app.core.models import EntityRef
from app.persistence import Database
from app.persistence.repositories import protocols
from tests.persistence import factories as f

DAY = timedelta(days=1)


def test_lookup_by_normalized_identity(seeded: Database) -> None:
    with seeded.transaction() as uow:
        assert uow.companies.get_by_domain(" Prospect.EXAMPLE ") == f.company()
        assert uow.contacts.get_by_email("PARTNERS@prospect.example") == f.contact()
        assert uow.contacts.list_by_company(f.COMPANY_ID) == [f.contact()]
        assert uow.companies.get("nope") is None


def test_lead_listing(seeded: Database) -> None:
    with seeded.transaction() as uow:
        assert uow.leads.list_by_contact(f.CONTACT_ID) == [f.lead()]
        assert uow.leads.list_by_campaign(f.CAMPAIGN_ID) == [f.lead()]
        assert uow.leads.list_by_campaign("other") == []
        assert uow.threads.list_by_lead(f.LEAD_ID) == [f.thread()]


def test_messages_listed_in_time_order(seeded: Database) -> None:
    later = f.inbound_message(message_id="msg-b", rfc_message_id="<b@x.example>", received_at=f.T0 + 2 * DAY)
    earlier = f.inbound_message(message_id="msg-a", rfc_message_id="<a@x.example>", received_at=f.T0 + DAY)
    sent = f.inbound_message(
        message_id="msg-c",
        rfc_message_id="<c@x.example>",
        direction=EmailDirection.OUTBOUND,
        received_at=None,
        sent_at=f.T0,
    )
    with seeded.transaction() as uow:
        for message in (later, earlier, sent):
            uow.messages.add(message)
    with seeded.transaction() as uow:
        assert [m.message_id for m in uow.messages.list_by_thread(f.THREAD_ID)] == ["msg-c", "msg-a", "msg-b"]


def test_outbound_listing(seeded: Database) -> None:
    with seeded.transaction() as uow:
        assert uow.outbound.list_by_lead(f.LEAD_ID) == [f.outbound_message()]
        assert uow.outbound.list_by_status(OutboundStatus.DRAFTED) == [f.outbound_message()]
        assert uow.outbound.list_by_status(OutboundStatus.SENT) == []


def test_dnc_active_respects_expiry_and_normalization(db: Database) -> None:
    permanent = f.dnc_entry()
    expiring = f.dnc_entry(entry_id="dnc-2", expires_at=f.T0 + DAY)
    domain = f.dnc_entry(entry_id="dnc-3", scope=DNCScope.DOMAIN, value="prospect.example")
    with db.transaction() as uow:
        for entry in (permanent, expiring, domain):
            uow.dnc.add(entry)
    with db.transaction() as uow:
        email = "Partners@PROSPECT.example"
        assert uow.dnc.list_active(DNCScope.EMAIL, email, f.T0) == [permanent, expiring]
        assert uow.dnc.list_active(DNCScope.EMAIL, email, f.T0 + DAY) == [permanent]
        assert uow.dnc.list_active(DNCScope.EMAIL, email, f.T0 - DAY) == []
        assert uow.dnc.list_for_value(DNCScope.EMAIL, email) == [permanent, expiring]
        assert uow.dnc.list_active(DNCScope.DOMAIN, "PROSPECT.example", f.T0) == [domain]
        assert uow.dnc.list_active(DNCScope.EMAIL, "other@prospect.example", f.T0) == []


def test_follow_up_queries(seeded: Database) -> None:
    plan = f.follow_up_plan(next_due_at=f.T0 + DAY)
    with seeded.transaction() as uow:
        uow.follow_ups.add(plan)
    with seeded.transaction() as uow:
        assert uow.follow_ups.get_open_for_lead(f.LEAD_ID) == plan
        assert uow.follow_ups.get_open_for_lead("other") is None
        assert uow.follow_ups.list_active_due(f.T0, limit=10) == []
        assert uow.follow_ups.list_active_due(f.T0 + DAY, limit=10) == [plan]
        with pytest.raises(ValueError):
            uow.follow_ups.list_active_due(f.T0, limit=0)
        uow.follow_ups.update(
            f.follow_up_plan(status=FollowUpStatus.PAUSED, next_due_at=f.T0 + DAY, version=2), 1
        )
        assert uow.follow_ups.list_active_due(f.T0 + DAY, limit=10) == []
        assert uow.follow_ups.get_open_for_lead(f.LEAD_ID) is not None


def test_escalation_listing(seeded: Database) -> None:
    with seeded.transaction() as uow:
        uow.escalations.add(f.escalation())
    with seeded.transaction() as uow:
        assert uow.escalations.list_by_lead(f.LEAD_ID) == [f.escalation()]
        assert uow.escalations.list_by_status(EscalationStatus.OPEN) == [f.escalation()]
        assert uow.escalations.list_by_status(EscalationStatus.RESOLVED) == []


def test_operator_responses_listed_in_order(seeded: Database) -> None:
    confirm = f.operator_response()
    done = f.operator_response(status=OperatorResponseStatus.OK, rendered_text="Paused.", created_at=f.T0 + DAY)
    with seeded.transaction() as uow:
        uow.operator_responses.add(done)
        uow.operator_responses.add(confirm)
    with seeded.transaction() as uow:
        assert uow.operator_responses.list_for_command(f.COMMAND_ID) == [confirm, done]


def test_knowledge_versions(db: Database) -> None:
    v1 = f.knowledge_source()
    v2 = f.knowledge_source(version=2, approval_status=KnowledgeApprovalStatus.RETIRED)
    other = f.knowledge_source(source_id="src-faq", domain=KnowledgeDomain.FAQ)
    with db.transaction() as uow:
        for source in (v2, v1, other):
            uow.knowledge_sources.add(source)
    with db.transaction() as uow:
        assert uow.knowledge_sources.get_latest("src-pricing") == v2
        assert uow.knowledge_sources.get("src-pricing", 1) == v1
        assert uow.knowledge_sources.list_by_domain(KnowledgeDomain.PRICING_COMMERCIAL) == [v1, v2]
        assert uow.knowledge_sources.get_latest("missing") is None


def test_audit_subject_query_is_exact(db: Database) -> None:
    with db.transaction() as uow:
        uow.audit.append(f.audit_event())
    with db.transaction() as uow:
        assert len(uow.audit.list_for_subject(EntityRef(kind=RefKind.CAMPAIGN, id=f.CAMPAIGN_ID))) == 1
        assert uow.audit.list_for_subject(EntityRef(kind=RefKind.LEAD, id="other")) == []
        assert uow.audit.list_for_subject(EntityRef(kind=RefKind.CAMPAIGN, id=f.LEAD_ID)) == []


@pytest.mark.parametrize(
    ("attribute", "protocol"),
    [
        ("companies", protocols.ProspectCompanyRepository),
        ("contacts", protocols.ProspectContactRepository),
        ("leads", protocols.LeadRepository),
        ("threads", protocols.EmailThreadRepository),
        ("messages", protocols.EmailMessageRepository),
        ("campaigns", protocols.CampaignRepository),
        ("outbound", protocols.OutboundMessageRepository),
        ("follow_ups", protocols.FollowUpPlanRepository),
        ("dnc", protocols.DoNotContactRepository),
        ("escalations", protocols.EscalationRepository),
        ("operator_commands", protocols.OperatorCommandRepository),
        ("operator_responses", protocols.OperatorResponseRepository),
        ("audit", protocols.AuditRepository),
        ("provenance", protocols.ProvenanceRepository),
        ("knowledge_sources", protocols.KnowledgeSourceMetaRepository),
        ("idempotency", protocols.IdempotencyRepository),
    ],
)
def test_unit_of_work_repositories_satisfy_protocols(
    db: Database, attribute: str, protocol: type
) -> None:
    with db.transaction() as uow:
        assert isinstance(getattr(uow, attribute), protocol)
