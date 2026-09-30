"""DNC stays the highest-priority stop, and Stages 6-11 keep their semantics with the
pipeline composed (reply engagement, operator commands, uncertain sends, follow-up and
campaign ownership, the runtime)."""

from pathlib import Path

import pytest

from app.core.enums import (
    BlockerCode,
    CampaignMemberStatus,
    CloseReason,
    ConversationStatus,
    DNCReason,
    DNCScope,
    LeadIntent,
    LeadStage,
    LostReason,
    NextActionOwner,
    NextActionType,
    OpportunityStatus,
    QualificationStatus,
    RefKind,
)
from app.core.models import DoNotContactEntry, EntityRef
from app.dispatch import DispatchRequest, FakeBehavior, FakeEmailTransport, FakeReconciler
from app.llm import LLMTask
from app.operator import CommandRejectedError, MarkLeadLost, StaleCommandError
from app.persistence import Database, FrozenClock
from app.pipeline import HookStatus, PipelineQueue
from app.pipeline.fake import FakeQualificationExtractor
from app.runtime import Adapters
from tests.campaign.builders import CAMPAIGN_ID, PROSPECT, approve as approve_touch, draft_touch, member, ready_campaign, scheduler
from tests.dispatch.builders import dispatcher, send
from tests.inbound.builders import NOW, SENDER, ScriptedTransport, classification, envelope, happy_transport, process
from tests.operator.builders import AS_ALICE
from tests.pipeline.builders import (
    REQUIRED,
    approve_qualification,
    create_opportunity,
    extraction,
    inbound,
    lead,
    mark_lost,
    mark_won,
    opportunity_lead,
    ops,
    pipeline,
    qualification,
    qualified_lead,
    qualifying_lead,
    reopen,
    start_negotiation,
)
from tests.runtime.builders import fake_adapters, runtime


def suppress(db: Database, email: str = SENDER) -> None:
    with db.transaction() as uow:
        uow.dnc.add(DoNotContactEntry(entry_id="dnc-" + email.split("@")[0], scope=DNCScope.EMAIL, value=email, reason=DNCReason.OPERATOR,
                                      source_ref=EntityRef(kind=RefKind.OPERATOR_COMMAND, id="c"), created_by="op",
                                      created_at=NOW))


def codes(error: CommandRejectedError) -> list[str]:
    return [c.value for c in error.codes]


def customer_says(db: Database, provider_message_id: str, intent: LeadIntent, body: str = "Thanks.") -> None:
    result = process(db, ScriptedTransport().script(LLMTask.INTENT_CLASSIFICATION, classification(intent)),
                     envelope(provider_message_id, body=body, in_reply_to="<p-1@prospect.example>"))
    pipeline(db).record_inbound(result, correlation_id=f"corr-{provider_message_id}")


# ---- DNC -------------------------------------------------------------------------------------------


def test_dnc_before_qualification_blocks_progress_and_extraction(db: Database) -> None:
    lead_id = qualifying_lead(db)
    suppress(db)
    with pytest.raises(CommandRejectedError) as error:
        approve_qualification(db, lead_id)
    assert codes(error.value) == ["CONTACT_SUPPRESSED"]
    result = process(db, ScriptedTransport().script(LLMTask.INTENT_CLASSIFICATION, classification(LeadIntent.INFO_REQUEST)),
                     envelope("p-2", in_reply_to="<p-1@prospect.example>"))
    outcome = pipeline(db, FakeQualificationExtractor(default=extraction(budget="1M"))).record_inbound(result, correlation_id="c")
    assert (outcome.status, outcome.reason) == (HookStatus.SKIPPED, "CONTACT_SUPPRESSED")
    view = pipeline(db).view(lead_id)
    assert view.suppressed and (view.next_action.owner, view.next_action.action) == (NextActionOwner.NONE, NextActionType.NONE)


def test_dnc_during_an_opportunity_blocks_negotiation_and_won_but_not_lost(db: Database) -> None:
    lead_id = opportunity_lead(db)
    suppress(db)
    with pytest.raises(CommandRejectedError) as error:
        start_negotiation(db, lead_id)
    assert codes(error.value) == ["CONTACT_SUPPRESSED"]
    with pytest.raises(CommandRejectedError):
        mark_won(db, lead_id)
    mark_lost(db, lead_id)
    assert lead(db, lead_id).close_reason is CloseReason.LOST


def test_reopening_a_suppressed_contact_is_refused_and_lost_does_not_remove_dnc(db: Database) -> None:
    lead_id = qualified_lead(db)
    mark_lost(db, lead_id)
    suppress(db)
    with pytest.raises(CommandRejectedError) as error:
        reopen(db, lead_id)
    assert codes(error.value) == ["CONTACT_SUPPRESSED"]
    with db.transaction() as uow:
        assert len(uow.dnc.list_active(DNCScope.EMAIL, SENDER, NOW)) == 1


def test_an_unsubscribe_during_negotiation_closes_the_lead_and_cancels_the_opportunity(db: Database) -> None:
    lead_id = opportunity_lead(db)
    start_negotiation(db, lead_id)
    customer_says(db, "p-unsub", LeadIntent.UNSUBSCRIBE, "Please unsubscribe me.")
    closed = lead(db, lead_id)
    assert (closed.stage, closed.close_reason) == (LeadStage.CLOSED, CloseReason.UNSUBSCRIBED)  # suppression wins
    with db.transaction() as uow:
        [opportunity] = uow.opportunities.list_by_lead(lead_id)
    assert opportunity.status is OpportunityStatus.CANCELLED  # no active opportunity on a closed lead
    with pytest.raises(CommandRejectedError) as error:
        reopen(db, lead_id)
    assert codes(error.value) in (["NOT_REOPENABLE"], ["CONTACT_SUPPRESSED"])


def test_reopen_never_restarts_campaign_automation(db: Database) -> None:
    member_id = ready_campaign(db)
    lead_id = member(db, member_id).lead_id or ""
    mark_lost(db, lead_id)
    reopen(db, lead_id)
    assert scheduler(db).schedule(CAMPAIGN_ID, correlation_id="c").scheduled == ()
    assert member(db, member_id).status is CampaignMemberStatus.CANCELLED


# ---- Cross-stage -------------------------------------------------------------------------------------


def test_stage6_reply_engages_the_lead_and_records_the_intent(db: Database) -> None:
    result = inbound(db, facts={})
    engaged = lead(db, result.lead_id or "")
    assert engaged.stage is LeadStage.INTERESTED and engaged.last_intent is LeadIntent.PRICING_REQUEST


def test_a_customer_declining_in_an_operator_stage_is_not_closed_automatically(db: Database) -> None:
    lead_id = qualified_lead(db)
    customer_says(db, "p-2", LeadIntent.NOT_INTERESTED, "We are not interested anymore.")
    still = lead(db, lead_id)
    assert (still.stage, still.last_intent) == (LeadStage.QUALIFIED, LeadIntent.NOT_INTERESTED)
    view = pipeline(db).view(lead_id)
    assert view.next_action.owner is NextActionOwner.OPERATOR and BlockerCode.CUSTOMER_DECLINED in view.next_action.blockers


def test_a_newer_customer_message_makes_an_operator_decision_stale(db: Database) -> None:
    lead_id = qualified_lead(db)
    seen_version = lead(db, lead_id).version
    customer_says(db, "p-2", LeadIntent.NEGOTIATION)
    with pytest.raises(StaleCommandError):
        ops(db).mark_lead_lost(AS_ALICE, MarkLeadLost(command_id="cmd-l", correlation_id="c", lead_id=lead_id,
                                                      expected_lead_version=seen_version, reason=LostReason.NO_BUDGET))


def test_an_uncertain_send_never_counts_as_contact(db: Database) -> None:
    member_id = ready_campaign(db)
    drafted = draft_touch(db)
    assert drafted.outbound_id is not None
    approve_touch(db, drafted.outbound_id)
    transport = FakeEmailTransport().script(FakeBehavior.ACCEPT_THEN_LOSE_RESPONSE)
    send(dispatcher(db, transport), drafted.outbound_id)
    lead_id = member(db, member_id).lead_id or ""
    assert lead(db, lead_id).stage is LeadStage.NEW  # UNKNOWN: no contact assumed
    view = pipeline(db).view(lead_id)
    assert view.next_action.action is NextActionType.AWAIT_DISPATCH_RESOLUTION
    reconciled = dispatcher(db, transport, reconciler=FakeReconciler(transport))
    reconciled.reconcile(DispatchRequest(outbound_id=drafted.outbound_id, correlation_id="c"))
    assert lead(db, lead_id).stage is LeadStage.CONTACTED  # positive evidence only


def test_conversation_and_campaign_ownership_are_unchanged(db: Database) -> None:
    member_id = ready_campaign(db)
    drafted = draft_touch(db)
    assert drafted.outbound_id is not None
    approve_touch(db, drafted.outbound_id)
    send(dispatcher(db), drafted.outbound_id)
    with db.transaction() as uow:
        touch = uow.outbound.get(drafted.outbound_id)
    assert touch is not None and touch.rfc_message_id is not None
    result = process(db, ScriptedTransport().script(LLMTask.INTENT_CLASSIFICATION, classification(LeadIntent.INFO_REQUEST)),
                     envelope("p-reply", sender=PROSPECT, in_reply_to=touch.rfc_message_id, references=(touch.rfc_message_id,)))
    with db.transaction() as uow:
        before = uow.conversations.get_by_thread(result.thread_id)
    pipeline(db, FakeQualificationExtractor(default=extraction(need="Invoice automation"))).record_inbound(result, correlation_id="c")
    with db.transaction() as uow:
        after = uow.conversations.get_by_thread(result.thread_id)
    assert member(db, member_id).status is CampaignMemberStatus.REPLIED  # Stage 10 handoff unchanged
    assert before is not None and after is not None and (after.status, after.version) == (before.status, before.version)
    assert lead(db, result.lead_id or "").stage is LeadStage.QUALIFYING  # the pipeline moved; the conversation did not


def test_runtime_ticks_never_advance_the_pipeline(db_path: Path) -> None:
    app = runtime(db_path, adapters=fake_adapters())
    app.start()
    for _ in range(3):
        app.tick(dispatch_approved=True)
    with Database(db_path) as db, db.transaction() as uow:
        assert uow.audit.list_by_event_type("PIPELINE_TRANSITION", 10) == []
    app.stop()


def test_runtime_inbound_runs_the_pipeline_hook(db_path: Path) -> None:
    base = fake_adapters(llm=happy_transport())
    adapters = Adapters(email_transport=base.email_transport, reconciler=base.reconciler, llm_transport=base.llm_transport,
                        authenticator=base.authenticator,
                        qualification_extractor=FakeQualificationExtractor(default=extraction(**REQUIRED)))
    app = runtime(db_path, adapters=adapters, clock=FrozenClock(NOW))
    app.start()
    result = app.handle_inbound(envelope("p-1"), correlation_id="c")
    view = app.services.pipeline.view(result.lead_id or "")
    assert (view.stage, view.qualification_status) == (LeadStage.QUALIFYING, QualificationStatus.READY_FOR_REVIEW)
    assert view.next_action.action is NextActionType.OPERATOR_REVIEW  # the Stage 6 draft awaits review first
    app.stop()


# ---- Read models ------------------------------------------------------------------------------------


def test_queues_and_metrics_come_from_durable_state(db: Database) -> None:
    ready = qualifying_lead(db)
    won = opportunity_lead_for(db, "won@prospect-a.example", "p-a")
    mark_won(db, won)
    lost = opportunity_lead_for(db, "lost@prospect-b.example", "p-b")
    mark_lost(db, lost)
    service = pipeline(db)
    assert ready in {v.lead_id for v in service.queue(PipelineQueue.NEEDS_OPERATOR)}
    assert {v.lead_id for v in service.queue(PipelineQueue.RECENTLY_CLOSED)} == {won, lost}
    assert service.queue(PipelineQueue.OPEN_OPPORTUNITIES) == ()
    numbers = service.metrics()
    assert (numbers.won, numbers.lost) == (1, 1)
    assert numbers.opportunities_by_status == {OpportunityStatus.WON: 1, OpportunityStatus.LOST: 1}
    assert numbers.qualification_by_status[QualificationStatus.READY_FOR_REVIEW] == 1
    assert any(t.to_stage == "CLOSED" and t.trigger == "OPERATOR_MARKED_WON" for t in numbers.transitions)
    view = service.view(ready)
    assert "how much does the Basic plan" not in view.model_dump_json()  # no message bodies in read models


def opportunity_lead_for(db: Database, sender: str, provider_message_id: str) -> str:
    result = inbound(db, provider_message_id, facts=REQUIRED, sender=sender)
    lead_id = result.lead_id or ""
    approve_qualification(db, lead_id, command_id=f"cmd-approve-{provider_message_id}")
    create_opportunity(db, lead_id, command_id=f"cmd-opp-{provider_message_id}")
    return lead_id


def test_a_v7_style_lead_without_pipeline_records_reads_cleanly(db: Database) -> None:
    result = process(db, happy_transport(), envelope("p-1"))
    view = pipeline(db).view(result.lead_id or "")
    assert (view.qualification_status, view.opportunity_id, view.conversation_status) == (
        QualificationStatus.NOT_STARTED, None, ConversationStatus.ACTIVE)
    assert qualification(db, result.lead_id or "") is None


# ---- Adversarial review regressions -------------------------------------------------------------


def test_a_replayed_older_message_never_rewinds_the_recorded_intent(db: Database) -> None:
    lead_id = qualified_lead(db)
    older = process(db, ScriptedTransport().script(LLMTask.INTENT_CLASSIFICATION, classification(LeadIntent.NOT_INTERESTED)),
                    envelope("p-2", body="Not now.", in_reply_to="<p-1@prospect.example>"))
    customer_says(db, "p-3", LeadIntent.NEGOTIATION)  # the newer message
    pipeline(db).record_inbound(older, correlation_id="replay")  # at-least-once: the older one arrives again
    assert lead(db, lead_id).last_intent is LeadIntent.NEGOTIATION


def test_no_opportunity_while_a_qualification_conflict_is_open(db: Database) -> None:
    lead_id = qualifying_lead(db, REQUIRED | {"budget": "50k EUR"})
    approve_qualification(db, lead_id)
    result = process(db, ScriptedTransport().script(LLMTask.INTENT_CLASSIFICATION, classification(LeadIntent.INFO_REQUEST)),
                     envelope("p-2", in_reply_to="<p-1@prospect.example>"))
    pipeline(db, FakeQualificationExtractor(default=extraction(budget="20k EUR"))).record_inbound(result, correlation_id="c")
    with pytest.raises(CommandRejectedError) as error:
        create_opportunity(db, lead_id)
    assert codes(error.value) == ["QUALIFICATION_CONFLICT_OPEN"]
