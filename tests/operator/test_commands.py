"""Reject, ownership, escalation resolution, idempotency, rollback and Stage 6 interaction."""

from datetime import timedelta

import pytest

from app.core.enums import (
    CampaignStatus,
    CloseReason,
    DNCScope,
    EscalationReason,
    EscalationResolution,
    EscalationStatus,
    FollowUpCancelReason,
    FollowUpStatus,
    KnowledgeDomain,
    LeadIntent,
    LeadStage,
    LeadStatus,
    OutboundDecision,
    OutboundStatus,
    ReplyDecision,
)
from app.core.models import Campaign, CampaignTargetFilter, FollowUpPlan
from app.inbound.knowledge_query import build_query
from app.knowledge import evaluate_knowledge
from app.llm import LLMTask
from app.operator import (
    BlockCode,
    CommandCollisionError,
    CommandRejectedError,
    RejectReason,
    StaleCommandError,
)
from app.operator import service as operator_service
from app.persistence import Database
from tests.inbound.builders import NOW, PRICE_QUESTION, SENDER, ComposerScript, ScriptedTransport, classification, envelope, process, sufficiency
from tests.inbound.test_threads_and_transitions import outbound_history
from tests.operator.builders import (
    ALICE,
    AS_ALICE,
    AS_BOB,
    approve_command,
    make_draft,
    make_escalation,
    operator,
    operator_events,
    outbound,
    ownership_command,
    reject_command,
    resolve_command,
    snapshot,
)


CAMPAIGN = Campaign(
    campaign_id="camp-1", name="Sample", status=CampaignStatus.ACTIVE, activated_by="op",
    target_filter=CampaignTargetFilter(), allowed_knowledge_domains=(KnowledgeDomain.COMPANY,),
    sending_mailbox="sales@ourco.example", max_follow_ups=2, min_interval_between_follow_ups=timedelta(days=2),
    created_by="op", created_at=NOW, updated_at=NOW,
)


def unsubscribe(db: Database, provider_message_id: str = "p-unsub") -> None:
    process(db, ScriptedTransport().script(LLMTask.INTENT_CLASSIFICATION, classification(LeadIntent.UNSUBSCRIBE)),
            envelope(provider_message_id, body="Please unsubscribe me."))


# ---- Reject ---------------------------------------------------------------------------------------


def test_reject_cancels_the_draft_with_a_reason(db: Database) -> None:
    draft = make_draft(db)
    service = operator(db)
    detail = service.get_draft(AS_ALICE, draft.outbound_id or "")
    result = service.reject_draft(AS_ALICE, reject_command(detail, reason=RejectReason.WRONG_TONE, note="Too pushy."))
    rejected = outbound(db, detail.outbound_id)
    assert rejected.status is OutboundStatus.CANCELLED and rejected.decision_reasons == ("OPERATOR_REJECTED", "WRONG_TONE")
    assert result.outcome.disposition == "REJECTED" and result.outcome.reason_codes == ("WRONG_TONE",)
    [event] = operator_events(db, "cmd-reject")
    assert event.after is not None and event.after["note"] == "Too pushy." and event.after["note_kind"] == "OPERATOR_REVIEW_CONTEXT"
    with pytest.raises(StaleCommandError):
        service.approve_draft(AS_ALICE, approve_command(detail))


def test_reject_of_an_already_decided_draft_is_explicit(db: Database) -> None:
    draft = make_draft(db)
    service = operator(db)
    service.approve_draft(AS_ALICE, approve_command(service.get_draft(AS_ALICE, draft.outbound_id or "")))
    current = service.get_draft(AS_ALICE, draft.outbound_id or "")
    with pytest.raises(CommandRejectedError) as error:
        service.reject_draft(AS_ALICE, reject_command(current))
    assert error.value.codes == (BlockCode.DRAFT_NOT_REVIEWABLE,)


# ---- Ownership --------------------------------------------------------------------------------------


def test_take_ownership_stops_follow_ups_and_is_version_checked(db: Database) -> None:
    draft = make_draft(db)
    lead_id = draft.lead_id or ""
    service = operator(db)
    lead = service.get_lead(AS_ALICE, lead_id)
    plan = FollowUpPlan(plan_id="plan-1", lead_id=lead_id, campaign_id="camp-1", anchor_outbound_id=draft.outbound_id or "",
                        max_steps=2, next_due_at=NOW + timedelta(days=2), created_at=NOW, updated_at=NOW)
    with db.transaction() as uow:
        uow.campaigns.add(CAMPAIGN)
        uow.follow_ups.add(plan)

    with pytest.raises(StaleCommandError):
        service.take_ownership(AS_ALICE, ownership_command(lead_id, lead.version + 1, "cmd-stale"))
    result = service.take_ownership(AS_ALICE, ownership_command(lead_id, lead.version))
    owned = service.get_lead(AS_ALICE, lead_id)
    assert owned.status is LeadStatus.OPERATOR_OWNED and owned.stage is lead.stage and owned.version == lead.version + 1
    with db.transaction() as uow:
        stored = uow.follow_ups.get("plan-1")
    assert stored is not None and (stored.status, stored.cancel_reason) == (FollowUpStatus.CANCELLED, FollowUpCancelReason.OPERATOR_TOOK_OVER)
    assert len(result.outcome.versions) == 2
    with pytest.raises(CommandRejectedError) as error:
        service.take_ownership(AS_BOB, ownership_command(lead_id, owned.version, "cmd-own-2"))
    assert error.value.codes == (BlockCode.LEAD_ALREADY_OWNED,)


def test_ownership_during_inbound_analysis_prevents_advance_and_new_drafts(db: Database) -> None:
    lead_id = outbound_history(db).lead_id  # CONTACTED; an automated pricing reply would advance it
    service = operator(db)

    def take_over() -> None:
        lead = service.get_lead(AS_ALICE, lead_id)
        service.take_ownership(AS_ALICE, ownership_command(lead_id, lead.version))

    transport = ScriptedTransport().script(LLMTask.INTENT_CLASSIFICATION, classification(LeadIntent.PRICING_REQUEST, PRICE_QUESTION))
    transport.script(LLMTask.KNOWLEDGE_SUFFICIENCY, sufficiency())
    transport.compose(ComposerScript(body="The Basic plan costs 100 EUR per month.", cite=lambda e: "100 EUR" in e["excerpt"], before=take_over))
    result = process(db, transport, envelope("p-2", body=PRICE_QUESTION, in_reply_to="<out-1@ourco.example>"))

    assert result.reply_decision is ReplyDecision.ESCALATE and result.draft_id is None
    assert result.escalation_reasons == (EscalationReason.STALE_ANALYSIS,)
    lead = service.get_lead(AS_ALICE, lead_id)
    assert (lead.status, lead.stage) == (LeadStatus.OPERATOR_OWNED, LeadStage.CONTACTED)  # ownership kept, not advanced
    assert service.list_pending_drafts(AS_ALICE) == ()


def test_closed_lead_cannot_be_taken_over(db: Database) -> None:
    draft = make_draft(db)
    unsubscribe(db)
    lead = operator(db).get_lead(AS_ALICE, draft.lead_id or "")
    with pytest.raises(CommandRejectedError) as error:
        operator(db).take_ownership(AS_ALICE, ownership_command(lead.lead_id, lead.version))
    assert error.value.codes == (BlockCode.LEAD_CLOSED,)


# ---- Escalation resolution ----------------------------------------------------------------------


def test_resolving_an_escalation_changes_nothing_else(db: Database) -> None:
    draft = make_draft(db, "p-1")
    escalation = make_escalation(db, "p-esc")  # same contact: lead goes ON_HOLD
    service = operator(db)
    lead_before = service.get_lead(AS_ALICE, escalation.lead_id or "")
    assert lead_before.status is LeadStatus.ON_HOLD
    draft_before = outbound(db, draft.outbound_id or "")

    service.resolve_escalation(AS_ALICE, resolve_command(escalation.escalation_id or "", note="Called them; price agreed offline."))
    detail = service.get_escalation(AS_ALICE, escalation.escalation_id or "")
    assert (detail.status, detail.resolution, detail.resolved_by) == (EscalationStatus.RESOLVED, EscalationResolution.NO_ACTION, ALICE)
    assert service.list_open_escalations(AS_ALICE) == ()
    lead_after = service.get_lead(AS_ALICE, escalation.lead_id or "")
    assert (lead_after.status, lead_after.stage, lead_after.version) == (lead_before.status, lead_before.stage, lead_before.version)
    assert outbound(db, draft.outbound_id or "") == draft_before  # no implicit approval


def test_resolution_never_reactivates_closed_or_suppressed_state(db: Database) -> None:
    escalation = make_escalation(db)
    unsubscribe(db)
    service = operator(db)
    service.resolve_escalation(AS_ALICE, resolve_command(escalation.escalation_id or ""))
    lead = service.get_lead(AS_ALICE, escalation.lead_id or "")
    assert (lead.stage, lead.close_reason) == (LeadStage.CLOSED, CloseReason.UNSUBSCRIBED)
    assert DNCScope.EMAIL in lead.suppressed_scopes


@pytest.mark.parametrize("disposition", [EscalationResolution.DRAFT_APPROVED, EscalationResolution.DRAFT_REJECTED])
def test_draft_dispositions_require_the_draft_commands(db: Database, disposition: EscalationResolution) -> None:
    escalation = make_escalation(db)
    with pytest.raises(CommandRejectedError) as error:
        operator(db).resolve_escalation(AS_ALICE, resolve_command(escalation.escalation_id or "", disposition=disposition))
    assert error.value.codes == (BlockCode.DISPOSITION_REQUIRES_DRAFT_COMMAND,)


def test_taken_over_disposition_requires_actual_ownership(db: Database) -> None:
    escalation = make_escalation(db)
    service = operator(db)
    command = resolve_command(escalation.escalation_id or "", disposition=EscalationResolution.TAKEN_OVER)
    with pytest.raises(CommandRejectedError) as error:
        service.resolve_escalation(AS_ALICE, command)
    assert error.value.codes == (BlockCode.LEAD_NOT_OWNED,)
    lead = service.get_lead(AS_ALICE, escalation.lead_id or "")
    service.take_ownership(AS_ALICE, ownership_command(lead.lead_id, lead.version))
    service.resolve_escalation(AS_ALICE, command)


def test_stale_or_repeated_resolution_is_explicit(db: Database) -> None:
    escalation = make_escalation(db)
    service = operator(db)
    with pytest.raises(StaleCommandError):
        service.resolve_escalation(AS_ALICE, resolve_command(escalation.escalation_id or "", version=2))
    service.resolve_escalation(AS_ALICE, resolve_command(escalation.escalation_id or ""))
    with pytest.raises(StaleCommandError):
        service.resolve_escalation(AS_BOB, resolve_command(escalation.escalation_id or "", command_id="cmd-resolve-2"))
    with pytest.raises(CommandRejectedError) as error:
        service.resolve_escalation(AS_BOB, resolve_command(escalation.escalation_id or "", version=2, command_id="cmd-resolve-3"))
    assert error.value.codes == (BlockCode.ESCALATION_NOT_OPEN,)


def test_operator_notes_never_become_knowledge(db: Database) -> None:
    escalation = make_escalation(db)
    note = "Samplewidget Co offers the Basic plan for 1 EUR per month forever."
    operator(db).resolve_escalation(AS_ALICE, resolve_command(escalation.escalation_id or "", note=note))
    plan = build_query(message_id="m-note", intent=LeadIntent.PRICING_REQUEST, questions=("What does the Basic plan cost per month?",),
                       locale="en", top_k=5, correlation_id="c", max_questions=5, max_chars=300)
    assert plan.query is not None
    with db.transaction() as uow:
        result = evaluate_knowledge(uow, plan.query, NOW)
    assert result.evidence and all("forever" not in e.excerpt for e in result.evidence)


# ---- Idempotency and rollback -------------------------------------------------------------------


def test_identical_replay_returns_the_recorded_outcome_without_writes(db: Database) -> None:
    draft = make_draft(db)
    service = operator(db)
    command = approve_command(service.get_draft(AS_ALICE, draft.outbound_id or ""))
    first = service.approve_draft(AS_ALICE, command)
    before = snapshot(db)
    again = service.approve_draft(AS_ALICE, command.model_copy(update={"correlation_id": "corr-retry"}))
    assert again.replayed and again.outcome == first.outcome
    assert snapshot(db) == before


def test_replay_after_cancellation_does_not_reactivate(db: Database) -> None:
    draft = make_draft(db)
    service = operator(db)
    command = approve_command(service.get_draft(AS_ALICE, draft.outbound_id or ""))
    service.approve_draft(AS_ALICE, command)
    unsubscribe(db)
    replay = service.approve_draft(AS_ALICE, command)
    assert replay.replayed and replay.outcome.disposition == "OPERATOR_APPROVED"  # history
    current = service.get_draft(AS_ALICE, draft.outbound_id or "")
    assert current.status is OutboundStatus.CANCELLED and not current.actionable  # current state


@pytest.mark.parametrize("variant", ["payload", "operator", "kind"])
def test_command_identity_collisions_fail_explicitly(db: Database, variant: str) -> None:
    draft = make_draft(db)
    service = operator(db)
    detail = service.get_draft(AS_ALICE, draft.outbound_id or "")
    service.approve_draft(AS_ALICE, approve_command(detail, "cmd-1"))
    before = snapshot(db)
    with pytest.raises(CommandCollisionError):
        if variant == "payload":
            service.approve_draft(AS_ALICE, approve_command(detail, "cmd-1", expected_lead_version=99))
        elif variant == "operator":
            service.approve_draft(AS_BOB, approve_command(detail, "cmd-1"))
        else:
            service.reject_draft(AS_ALICE, reject_command(detail, "cmd-1"))
    assert snapshot(db) == before


def test_failure_after_the_transition_rolls_everything_back(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    draft = make_draft(db)
    service = operator(db)
    command = approve_command(service.get_draft(AS_ALICE, draft.outbound_id or ""))
    before = snapshot(db)

    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("audit store unavailable")

    monkeypatch.setattr(operator_service, "_command_event", broken)
    with pytest.raises(RuntimeError):
        service.approve_draft(AS_ALICE, command)
    assert snapshot(db) == before  # no decision, no audit, no idempotency reservation
    monkeypatch.undo()
    assert not service.approve_draft(AS_ALICE, command).replayed  # the retry applies normally


# ---- Stage 6 unsubscribe interaction --------------------------------------------------------------


def test_approval_then_unsubscribe_cancels_the_approved_reply(db: Database) -> None:
    draft = make_draft(db)
    service = operator(db)
    service.approve_draft(AS_ALICE, approve_command(service.get_draft(AS_ALICE, draft.outbound_id or "")))
    assert outbound(db, draft.outbound_id or "").status is OutboundStatus.OPERATOR_APPROVED
    unsubscribe(db)
    cancelled = outbound(db, draft.outbound_id or "")
    assert cancelled.status is OutboundStatus.CANCELLED and cancelled.approved_at == NOW  # history kept on the row
    with db.transaction() as uow:
        assert len(uow.dnc.list_active(DNCScope.EMAIL, SENDER, NOW)) == 1


def test_unsubscribe_then_approval_is_refused(db: Database) -> None:
    draft = make_draft(db)
    service = operator(db)
    detail = service.get_draft(AS_ALICE, draft.outbound_id or "")
    unsubscribe(db)
    with pytest.raises(StaleCommandError) as error:
        service.approve_draft(AS_ALICE, approve_command(detail))
    assert {BlockCode.DRAFT_VERSION_CHANGED, BlockCode.CONTACT_SUPPRESSED, BlockCode.LEAD_CLOSED} <= set(error.value.codes)
    assert outbound(db, draft.outbound_id or "").status is OutboundStatus.CANCELLED


def test_unsubscribe_never_touches_dispatched_history(db: Database) -> None:
    draft = make_draft(db)
    sent_like = outbound(db, draft.outbound_id or "").model_copy(
        update={"status": OutboundStatus.SENT, "decision": OutboundDecision.SEND, "send_permit_id": "permit-1", "approved_at": NOW,
                "sending_at": NOW, "sent_at": NOW, "version": 2}
    )
    with db.transaction() as uow:
        uow.outbound.update(sent_like, 1)
    unsubscribe(db)
    assert outbound(db, draft.outbound_id or "").status is OutboundStatus.SENT
