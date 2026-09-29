from datetime import timedelta

from app.core.enums import (
    CloseReason,
    ContactDepartment,
    ContactSource,
    ContactType,
    EmailDirection,
    EscalationReason,
    LeadIntent,
    LeadOrigin,
    LeadStage,
    LeadStatus,
    ReplyDecision,
)
from app.core.models import EmailMessage, EmailThread, Lead, ProspectCompany, ProspectContact
from app.llm import LLMTask
from app.persistence import Database
from tests.inbound.builders import (
    MAILBOX,
    NOW,
    PRICE_QUESTION,
    SENDER,
    ScriptedTransport,
    classification,
    envelope,
    happy_transport,
    process,
)

C = LLMTask.INTENT_CLASSIFICATION
HASH = "f" * 64


def outbound_history(db: Database, *, stage: LeadStage = LeadStage.CONTACTED, thread_id: str = "th-out", rfc: str = "<out-1@ourco.example>") -> Lead:
    """A prior outbound touch: company, contact, CONTACTED lead, thread and our sent message."""
    lead = Lead(
        lead_id="lead-out", contact_id="contact-out", company_id="company-out", origin=LeadOrigin.OUTBOUND,
        stage=stage, created_at=NOW - timedelta(days=5), updated_at=NOW - timedelta(days=5),
    )
    with db.transaction() as uow:
        if uow.companies.get("company-out") is None:
            uow.companies.add(ProspectCompany(company_id="company-out", name="Prospect Ltd", domain="prospect.example", source=ContactSource.IMPORT, created_at=NOW, updated_at=NOW))
            uow.contacts.add(ProspectContact(contact_id="contact-out", company_id="company-out", email=SENDER, department=ContactDepartment.PARTNERSHIPS, contact_type=ContactType.NAMED_BUSINESS, source=ContactSource.IMPORT, collected_at=NOW, created_at=NOW, updated_at=NOW))
            uow.leads.add(lead)
        uow.threads.add(EmailThread(thread_id=thread_id, mailbox=MAILBOX, participant_addresses=(MAILBOX, SENDER), subject_normalized="partnership", lead_id=lead.lead_id, message_ids=(f"m-{thread_id}",)))
        uow.messages.add(EmailMessage(message_id=f"m-{thread_id}", rfc_message_id=rfc, thread_id=thread_id, direction=EmailDirection.OUTBOUND, mailbox=MAILBOX, from_address=MAILBOX, to_addresses=(SENDER,), subject="Partnership", body_text="Hello", raw_ref="raw/out", raw_hash=HASH, sent_at=NOW - timedelta(days=5)))
    return lead


# ---- 47. Threads -----------------------------------------------------------------------------------


def test_in_reply_to_joins_the_thread_and_its_lead(db: Database) -> None:
    lead = outbound_history(db)
    result = process(db, happy_transport(), envelope(in_reply_to="<out-1@ourco.example>"))
    assert (result.thread_id, result.lead_id) == ("th-out", lead.lead_id)
    with db.transaction() as uow:
        thread = uow.threads.get("th-out")
        assert thread is not None and thread.message_ids[-1] == result.message_id and thread.version == 2
        assert thread.last_inbound_at == NOW


def test_references_join_the_thread(db: Database) -> None:
    outbound_history(db)
    result = process(db, happy_transport(), envelope(references=("<unknown@x.example>", "<out-1@ourco.example>")))
    assert result.thread_id == "th-out"


def test_same_subject_and_sender_do_not_merge_threads(db: Database) -> None:
    first = process(db, happy_transport(), envelope("p-1", subject="Pricing question"))
    second = process(db, ScriptedTransport().script(C, classification(LeadIntent.INFO_REQUEST, "Do you support blockchain tokens?")), envelope("p-2", subject="Pricing question"))
    assert first.thread_id != second.thread_id


def test_ambiguous_references_fail_closed(db: Database) -> None:
    outbound_history(db)
    outbound_history(db, thread_id="th-other", rfc="<out-2@ourco.example>")
    transport = ScriptedTransport()
    result = process(db, transport, envelope(in_reply_to="<out-1@ourco.example>", references=("<out-2@ourco.example>",)))
    assert (result.reply_decision, result.escalation_reasons) == (ReplyDecision.ESCALATE, (EscalationReason.AMBIGUOUS_THREAD,))
    assert result.thread_id not in ("th-out", "th-other")
    assert transport.requests == []


# ---- 49. Lead transitions (Stage 1 table is authoritative) ------------------------------------------


def lead_after(db: Database, lead_id: str) -> Lead:
    with db.transaction() as uow:
        lead = uow.leads.get(lead_id)
    assert lead is not None
    return lead


def test_contacted_lead_advances_through_engaged_to_interested(db: Database) -> None:
    lead = outbound_history(db)
    process(db, happy_transport(), envelope(in_reply_to="<out-1@ourco.example>"))
    updated = lead_after(db, lead.lead_id)
    assert (updated.stage, updated.version, updated.company_id) == (LeadStage.INTERESTED, 2, "company-out")


def test_meeting_request_moves_to_meeting_requested(db: Database) -> None:
    lead = outbound_history(db)
    transport = ScriptedTransport().script(C, classification(LeadIntent.MEETING_REQUEST, "Can we book a demo call?"))
    result = process(db, transport, envelope(body="Can we book a demo call?", in_reply_to="<out-1@ourco.example>"))
    # No approved meeting guidance exists in the fixture KB, so it escalates, but the
    # stage still records the prospect's request.
    assert result.reply_decision is ReplyDecision.ESCALATE
    updated = lead_after(db, lead.lead_id)
    assert (updated.stage, updated.status) == (LeadStage.MEETING_REQUESTED, LeadStatus.ON_HOLD)


def test_positive_interest_moves_to_interested(db: Database) -> None:
    lead = outbound_history(db)
    transport = ScriptedTransport().script(C, classification(LeadIntent.POSITIVE_INTEREST, "Sounds great, what integrations does the Sample Widget support?"))
    process(db, transport, envelope(in_reply_to="<out-1@ourco.example>"))
    assert lead_after(db, lead.lead_id).stage is LeadStage.INTERESTED


def test_invalid_proposed_transition_is_rejected(db: Database) -> None:
    lead = outbound_history(db, stage=LeadStage.MEETING_REQUESTED)
    transport = ScriptedTransport().script(C, classification(LeadIntent.PRICING_REQUEST, PRICE_QUESTION, proposed_stage="CONTACTED"))
    result = process(db, transport, envelope(in_reply_to="<out-1@ourco.example>"))
    assert result.escalation_reasons == (EscalationReason.INVALID_LEAD_TRANSITION,)
    updated = lead_after(db, lead.lead_id)
    assert updated.stage is LeadStage.MEETING_REQUESTED  # never moved backwards


def test_never_moves_backwards(db: Database) -> None:
    lead = outbound_history(db, stage=LeadStage.MEETING_REQUESTED)
    process(db, happy_transport(), envelope(in_reply_to="<out-1@ourco.example>"))
    assert lead_after(db, lead.lead_id).stage is LeadStage.MEETING_REQUESTED


def test_not_interested_closes_an_existing_lead(db: Database) -> None:
    lead = outbound_history(db)
    process(db, ScriptedTransport().script(C, classification(LeadIntent.NOT_INTERESTED)), envelope(in_reply_to="<out-1@ourco.example>"))
    closed = lead_after(db, lead.lead_id)
    assert (closed.stage, closed.close_reason) == (LeadStage.CLOSED, CloseReason.NOT_INTERESTED)


def test_known_company_is_linked_only_when_it_already_exists(db: Database) -> None:
    outbound_history(db)
    result = process(db, happy_transport(), envelope(sender="colleague@prospect.example"))
    with db.transaction() as uow:
        contact = uow.contacts.get_by_email("colleague@prospect.example")
        lead = uow.leads.get(result.lead_id or "")
    assert contact is not None and contact.company_id == "company-out"  # existing domain match
    assert lead is not None and lead.company_id == "company-out" and lead.origin is LeadOrigin.INBOUND
