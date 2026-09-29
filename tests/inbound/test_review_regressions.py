"""Regression tests for the Stage 6 targeted review (question coverage, thread ownership,
finalization revalidation, races between different messages for one lead)."""

import threading
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

from app.core.enums import (
    CampaignStatus,
    CloseReason,
    DNCReason,
    DNCScope,
    EscalationReason,
    KnowledgeDomain,
    LeadIntent,
    LeadStage,
    LeadStatus,
    OutboundStatus,
    RefKind,
    ReplyDecision,
)
from app.core.models import Campaign, CampaignTargetFilter, DoNotContactEntry, EntityRef, Lead
from app.inbound import InboundResult
from app.knowledge import ingest_loaded, parse_source_text
from app.llm import LLMTask
from app.persistence import Database, FrozenClock
from tests.inbound.builders import (
    NOW,
    PRICE_QUESTION,
    SENDER,
    ComposerScript,
    ScriptedTransport,
    classification,
    envelope,
    happy_transport,
    process,
    service,
    sufficiency,
)
from tests.inbound.test_threads_and_transitions import outbound_history
from tests.knowledge.sources import meta, yaml_doc

C, S, P = LLMTask.INTENT_CLASSIFICATION, LLMTask.KNOWLEDGE_SUFFICIENCY, LLMTask.REPLY_COMPOSITION
def price_cite(evidence: dict[str, str]) -> bool:
    return "100 EUR" in evidence["excerpt"]


def pricing_transport(*questions: str, before: Callable[[], None] | None = None) -> ScriptedTransport:
    transport = ScriptedTransport().script(C, classification(LeadIntent.PRICING_REQUEST, *(questions or (PRICE_QUESTION,))))
    transport.script(S, sufficiency())
    transport.compose(ComposerScript(body="The Basic plan costs 100 EUR per month.", cite=price_cite, before=before))
    return transport


def escalation_context(db: Database, result: InboundResult) -> dict[str, object]:
    with db.transaction() as uow:
        events = uow.audit.list_for_subject(EntityRef(kind=RefKind.ESCALATION, id=result.escalation_id or ""))
    [event] = [e for e in events if e.event_type == "ESCALATION_CREATED"]
    assert event.after is not None
    return dict(event.after)


def drafts(db: Database, lead_id: str) -> list[OutboundStatus]:
    with db.transaction() as uow:
        return [m.status for m in uow.outbound.list_by_lead(lead_id)]


def lead(db: Database, lead_id: str) -> Lead:
    with db.transaction() as uow:
        found = uow.leads.get(lead_id)
    assert found is not None
    return found


# ---- 1. Complete question coverage ---------------------------------------------------------------


def test_questions_beyond_the_cap_fail_closed(db: Database) -> None:
    questions = [PRICE_QUESTION, "What does the Team plan cost per month?", "Are invoices issued monthly?",
                 "Is billing in EUR?", "Does the Basic plan cost the same every month?", "Do you support blockchain tokens?"]
    transport = pricing_transport(*questions)
    result = process(db, transport)
    assert (result.reply_decision, result.escalation_reasons) == (ReplyDecision.ESCALATE, (EscalationReason.UNASSESSED_QUESTIONS,))
    assert transport.calls(P) == 0 and result.draft_id is None
    assert escalation_context(db, result)["omitted_questions"] == ["Do you support blockchain tokens?"]
    with db.transaction() as uow:
        assert uow.messages.get(result.message_id) is not None  # original preserved for the operator


def test_overlong_question_is_not_silently_dropped(db: Database) -> None:
    overlong = "Could you explain in full detail " + "how blockchain tokens work " * 12 + "?"
    result = process(db, pricing_transport(PRICE_QUESTION, overlong))
    assert result.escalation_reasons == (EscalationReason.UNASSESSED_QUESTIONS,)
    assert escalation_context(db, result)["omitted_questions"] == [" ".join(overlong.split())]


def test_equivalent_questions_are_legitimately_merged(db: Database) -> None:
    result = process(db, pricing_transport(PRICE_QUESTION, "  what does the BASIC plan cost per   month? "))
    assert result.reply_decision is ReplyDecision.DRAFT_FOR_REVIEW


def test_question_without_searchable_terms_is_assessed_not_dropped(db: Database) -> None:
    result = process(db, pricing_transport(PRICE_QUESTION, "Is it?"))
    assert result.escalation_reasons == (EscalationReason.KNOWLEDGE_INSUFFICIENT,)


# ---- 2. Thread and sender ownership --------------------------------------------------------------


def test_forged_reference_from_unknown_sender_fails_closed(db: Database) -> None:
    other = outbound_history(db)
    transport = ScriptedTransport()
    result = process(db, transport, envelope(sender="stranger@elsewhere.example", in_reply_to="<out-1@ourco.example>"))
    assert result.escalation_reasons == (EscalationReason.UNVERIFIED_THREAD_REFERENCE,)
    assert result.thread_id != "th-out" and result.lead_id != other.lead_id
    assert transport.requests == []  # no LLM saw the other contact's history
    untouched = lead(db, other.lead_id)
    assert (untouched.stage, untouched.status, untouched.version) == (LeadStage.CONTACTED, LeadStatus.AUTOMATED, 1)
    with db.transaction() as uow:
        thread = uow.threads.get("th-out")
        assert thread is not None and result.message_id not in thread.message_ids and thread.version == 1


def test_reference_to_another_known_contacts_thread_fails_closed(db: Database) -> None:
    outbound_history(db)
    process(db, happy_transport(), envelope("p-9", sender="other@prospect.example"))  # a second known contact
    result = process(db, ScriptedTransport(), envelope("p-10", sender="other@prospect.example", references=("<out-1@ourco.example>",)))
    assert result.escalation_reasons == (EscalationReason.UNVERIFIED_THREAD_REFERENCE,)
    assert result.thread_id != "th-out"


def test_participant_reply_still_joins(db: Database) -> None:
    outbound_history(db)
    assert process(db, happy_transport(), envelope(in_reply_to="<out-1@ourco.example>")).thread_id == "th-out"


# ---- 3. Finalization revalidation -----------------------------------------------------------------


def test_dnc_added_during_analysis_blocks_the_draft(db: Database) -> None:
    def add_dnc() -> None:
        with db.transaction() as uow:
            uow.dnc.add(DoNotContactEntry(entry_id="dnc-op", scope=DNCScope.EMAIL, value=SENDER, reason=DNCReason.OPERATOR,
                                          source_ref=EntityRef(kind=RefKind.OPERATOR_COMMAND, id="cmd-1"), created_by="operator", created_at=NOW))

    result = process(db, pricing_transport(before=add_dnc))
    assert result.escalation_reasons == (EscalationReason.STALE_ANALYSIS,) and result.draft_id is None
    assert "suppressed" in str(escalation_context(db, result)["detail"])


def test_operator_hold_during_analysis_blocks_draft_and_stage_advance(db: Database) -> None:
    outbound_history(db)

    def hold() -> None:
        with db.transaction() as uow:
            current = uow.leads.get("lead-out")
            assert current is not None
            uow.leads.update(current.model_copy(update={"status": LeadStatus.ON_HOLD, "version": 2}), 1)

    result = process(db, pricing_transport(before=hold), envelope(in_reply_to="<out-1@ourco.example>"))
    assert result.escalation_reasons == (EscalationReason.STALE_ANALYSIS,)
    held = lead(db, "lead-out")
    assert (held.stage, held.status) == (LeadStage.CONTACTED, LeadStatus.ON_HOLD)  # not advanced


def test_lead_closed_during_analysis_is_never_reopened(db: Database) -> None:
    outbound_history(db)

    def close() -> None:
        with db.transaction() as uow:
            current = uow.leads.get("lead-out")
            assert current is not None
            uow.leads.update(current.model_copy(update={"stage": LeadStage.CLOSED, "close_reason": CloseReason.WON, "version": 2}), 1)

    result = process(db, pricing_transport(before=close), envelope(in_reply_to="<out-1@ourco.example>"))
    assert result.escalation_reasons == (EscalationReason.STALE_ANALYSIS,)
    closed = lead(db, "lead-out")
    assert (closed.stage, closed.close_reason, closed.version) == (LeadStage.CLOSED, CloseReason.WON, 2)


def test_campaign_paused_during_analysis_blocks_the_draft(db: Database) -> None:
    outbound_history(db)
    campaign = Campaign(campaign_id="camp-x", name="Sample", status=CampaignStatus.ACTIVE, activated_by="op",
                        target_filter=CampaignTargetFilter(), allowed_knowledge_domains=(KnowledgeDomain.COMPANY,),
                        sending_mailbox="sales@ourco.example", max_follow_ups=1, min_interval_between_follow_ups=timedelta(days=2),
                        created_by="op", created_at=NOW, updated_at=NOW)
    with db.transaction() as uow:
        uow.campaigns.add(campaign)
        current = uow.leads.get("lead-out")
        assert current is not None
        uow.leads.update(current.model_copy(update={"campaign_id": "camp-x", "version": 2}), 1)

    def pause() -> None:
        with db.transaction() as uow:
            uow.campaigns.update(campaign.model_copy(update={"status": CampaignStatus.PAUSED, "version": 2}), 1)

    result = process(db, pricing_transport(before=pause), envelope(in_reply_to="<out-1@ourco.example>"))
    assert result.escalation_reasons == (EscalationReason.STALE_ANALYSIS,)


def test_evidence_retired_during_analysis_blocks_the_draft(db: Database) -> None:
    def retire() -> None:
        text = yaml_doc(meta(source_id="sample-price-list", domain="PRICING_COMMERCIAL", version=3, approval_status="RETIRED",
                             effective_from="2026-02-01T00:00:00+00:00", review_by="2026-08-01T00:00:00+00:00"),
                        body="## Retired\nThis price list is retired.")
        with db.transaction() as uow:
            ingest_loaded(uow, parse_source_text(text, extension=".yaml", label="pricing/retired.yaml"), now=NOW)

    result = process(db, pricing_transport(before=retire))
    assert result.escalation_reasons == (EscalationReason.STALE_ANALYSIS,)
    assert "evidence" in str(escalation_context(db, result)["detail"])


def test_evidence_expiring_before_finalization_uses_the_injected_clock(db: Database) -> None:
    clock = FrozenClock(NOW)
    transport = pricing_transport(before=lambda: clock.set(NOW.replace(month=8, day=2)))  # price list review_by is 2026-08-01
    result = service(db, transport, clock=clock).process(envelope(), correlation_id="corr-1")
    assert result.escalation_reasons == (EscalationReason.STALE_ANALYSIS,)


# ---- 4. Different messages for the same lead ------------------------------------------------------


def unsubscribe_transport() -> ScriptedTransport:
    return ScriptedTransport().script(C, classification(LeadIntent.UNSUBSCRIBE))


def test_unsubscribe_finishing_first_blocks_the_stale_sales_draft(db: Database) -> None:
    def concurrent_unsubscribe() -> None:
        process(db, unsubscribe_transport(), envelope("p-unsub", body="Please unsubscribe me."))

    result = process(db, pricing_transport(before=concurrent_unsubscribe), envelope("p-sales"))
    assert result.escalation_reasons == (EscalationReason.STALE_ANALYSIS,) and result.draft_id is None
    closed = lead(db, result.lead_id or "")
    assert (closed.stage, closed.close_reason) == (LeadStage.CLOSED, CloseReason.UNSUBSCRIBED)
    with db.transaction() as uow:
        assert len(uow.dnc.list_active(DNCScope.EMAIL, SENDER, NOW)) == 1


def test_unsubscribe_after_a_draft_cancels_it(db: Database) -> None:
    sales = process(db, pricing_transport(), envelope("p-sales"))
    assert drafts(db, sales.lead_id or "") == [OutboundStatus.DRAFTED]
    process(db, unsubscribe_transport(), envelope("p-unsub", body="Please unsubscribe me."))
    assert drafts(db, sales.lead_id or "") == [OutboundStatus.CANCELLED]
    assert lead(db, sales.lead_id or "").close_reason is CloseReason.UNSUBSCRIBED


def test_threaded_race_between_sales_and_unsubscribe(db_path: Path) -> None:
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def worker(pid: str, body: str, make: Callable[[], ScriptedTransport]) -> None:
        with Database(db_path, busy_timeout_ms=10_000) as worker_db:
            transport = make()
            barrier.wait()
            try:
                service(worker_db, transport).process(envelope(pid, body=body), correlation_id=pid)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=("p-sales", "How much does the Basic plan cost per month?", pricing_transport)),
        threading.Thread(target=worker, args=("p-unsub", "Please unsubscribe me.", unsubscribe_transport)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert errors == []
    with Database(db_path) as check, check.transaction() as uow:
        assert len(uow.dnc.list_active(DNCScope.EMAIL, SENDER, NOW)) == 1
        contact = uow.contacts.get_by_email(SENDER)
        assert contact is not None
        leads = uow.leads.list_by_contact(contact.contact_id)
        # The unsubscribe's lead is always closed. If the unsubscribe completed before the sales
        # message was even observed, that message is a new inbound from a suppressed contact: it
        # gets its own lead, which is held for an operator, never drafted for, never reopened.
        assert any((found.stage, found.close_reason) == (LeadStage.CLOSED, CloseReason.UNSUBSCRIBED) for found in leads)
        for found in leads:
            assert found.stage is LeadStage.CLOSED or found.status is LeadStatus.ON_HOLD
            assert found.stage is not LeadStage.CLOSED or found.close_reason is CloseReason.UNSUBSCRIBED
            assert OutboundStatus.DRAFTED not in [m.status for m in uow.outbound.list_by_lead(found.lead_id)]


def test_unsubscribe_is_recognized_on_early_escalation_paths(db: Database) -> None:
    attachment = process(db, ScriptedTransport(), envelope("p-1", body="Please unsubscribe me.", has_attachments=True))
    assert attachment.escalation_reasons == (EscalationReason.UNSUPPORTED_CONTENT,)
    with db.transaction() as uow:
        assert len(uow.dnc.list_active(DNCScope.EMAIL, SENDER, NOW)) == 1


def test_unsubscribe_is_recorded_even_when_the_classifier_fails(db: Database) -> None:
    transport = ScriptedTransport()  # nothing scripted: the classifier call fails
    result = process(db, transport, envelope(body="Stop emailing me."))
    assert result.escalation_reasons == (EscalationReason.CLASSIFIER_FAILURE,)
    with db.transaction() as uow:
        assert len(uow.dnc.list_active(DNCScope.EMAIL, SENDER, NOW)) == 1


def test_unsubscribe_on_a_forged_reference_suppresses_the_sender_only(db: Database) -> None:
    other = outbound_history(db)
    result = process(db, ScriptedTransport(), envelope(sender="stranger@elsewhere.example", body="Unsubscribe me.", in_reply_to="<out-1@ourco.example>"))
    assert result.escalation_reasons == (EscalationReason.UNVERIFIED_THREAD_REFERENCE,)
    with db.transaction() as uow:
        assert len(uow.dnc.list_active(DNCScope.EMAIL, "stranger@elsewhere.example", NOW)) == 1
        assert uow.dnc.list_active(DNCScope.EMAIL, SENDER, NOW) == []
    assert lead(db, other.lead_id).stage is LeadStage.CONTACTED


def test_negated_unsubscribe_wording_is_not_an_unsubscribe(db: Database) -> None:
    process(db, pricing_transport(), envelope(body="Please don't unsubscribe me. What does the Basic plan cost per month?"))
    with db.transaction() as uow:
        assert uow.dnc.list_for_value(DNCScope.EMAIL, SENDER) == []
