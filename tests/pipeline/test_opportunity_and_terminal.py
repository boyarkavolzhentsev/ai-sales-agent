"""Opportunities, WON/LOST/disqualify, reopen, terminal automation effects, idempotency."""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.core.enums import (
    CampaignJobStatus,
    CampaignMemberStatus,
    CloseReason,
    ConversationStatus,
    DisqualificationReason,
    FollowUpJobStatus,
    LeadStage,
    LeadStatus,
    LostReason,
    OpportunityStatus,
    OutboundDecision,
    OutboundStatus,
    QualificationStatus,
)
from app.core.models import OutboundMessage
from app.operator import (
    CommandCollisionError,
    CommandRejectedError,
    CreateOpportunity,
    MarkLeadLost,
    OperatorUnauthorizedError,
    StaleCommandError,
)
from app.persistence import Database, QuotaReservationState
from app.policy import reserve_quota
from tests.campaign.builders import CAMPAIGN_ID, approve as approve_campaign_draft, draft_touch, member, ready_campaign, scheduler
from tests.conversation.builders import replied_conversation, scheduler as follow_up_scheduler
from tests.inbound.builders import NOW, envelope, happy_transport, process
from tests.operator.builders import AS_ALICE, credential
from tests.pipeline.builders import (
    active_opportunity,
    create_opportunity,
    disqualify,
    known,
    lead,
    mark_lost,
    mark_won,
    ops,
    opportunity,
    opportunity_lead,
    pipeline,
    qualification,
    qualified_lead,
    qualifying_lead,
    reopen,
    start_negotiation,
)
from tests.policy import builders as policy


def codes(error: CommandRejectedError) -> list[str]:
    return [c.value for c in error.codes]


def status(db: Database, lead_id: str) -> QualificationStatus:
    found = qualification(db, lead_id)
    assert found is not None
    return found.status


# ---- Opportunity ---------------------------------------------------------------------------------


def test_there_is_no_opportunity_until_an_operator_creates_one(db: Database) -> None:
    lead_id = qualified_lead(db)
    assert pipeline(db).view(lead_id).opportunity_id is None
    create_opportunity(db, lead_id)
    opp = active_opportunity(db, lead_id)
    assert (opp.status, opp.amount, opp.currency, opp.expected_decision_date) == (OpportunityStatus.OPEN, None, None, None)
    assert lead(db, lead_id).stage is LeadStage.OPPORTUNITY and not hasattr(opp, "probability")


def test_known_commercial_data_is_kept_and_half_known_money_is_refused(db: Database) -> None:
    lead_id = opportunity_lead(db, **known("12500.00"))
    assert active_opportunity(db, lead_id).amount == Decimal("12500.00")
    with pytest.raises(ValidationError):
        CreateOpportunity(command_id="c1", correlation_id="c", lead_id=lead_id, expected_lead_version=1, amount=Decimal("1"))


def test_an_opportunity_needs_an_approved_qualification(db: Database) -> None:
    lead_id = qualifying_lead(db)
    with pytest.raises(CommandRejectedError) as error:
        create_opportunity(db, lead_id)
    assert codes(error.value) == ["QUALIFICATION_NOT_READY"]


def test_only_one_active_opportunity_per_lead(db: Database) -> None:
    lead_id = opportunity_lead(db)
    with pytest.raises(CommandRejectedError) as error:
        create_opportunity(db, lead_id, command_id="cmd-opportunity-2")
    assert codes(error.value) == ["OPPORTUNITY_EXISTS"]


def test_negotiation_then_won_closes_lead_and_opportunity(db: Database) -> None:
    lead_id = opportunity_lead(db)
    start_negotiation(db, lead_id)
    assert (lead(db, lead_id).stage, active_opportunity(db, lead_id).status) == (LeadStage.NEGOTIATION, OpportunityStatus.NEGOTIATING)
    opp_id = active_opportunity(db, lead_id).opportunity_id
    result = mark_won(db, lead_id)
    closed = lead(db, lead_id)
    assert (closed.stage, closed.close_reason, result.outcome.disposition) == (LeadStage.CLOSED, CloseReason.WON, "WON")
    won = opportunity(db, opp_id)
    assert won.status is OpportunityStatus.WON and won.closed_at is not None
    assert result.outcome.operator_id == "op-alice" and result.outcome.correlation_id == "c"


def test_lost_closes_the_active_opportunity_with_its_reason(db: Database) -> None:
    lead_id = opportunity_lead(db)
    opp_id = active_opportunity(db, lead_id).opportunity_id
    mark_lost(db, lead_id)
    assert (lead(db, lead_id).close_reason, opportunity(db, opp_id).status) == (CloseReason.LOST, OpportunityStatus.LOST)
    assert opportunity(db, opp_id).lost_reason is not None


def test_terminal_decisions_require_an_authorized_operator(db: Database) -> None:
    lead_id = opportunity_lead(db)
    with pytest.raises(OperatorUnauthorizedError):
        ops(db).mark_lead_lost(credential("not-a-token"), MarkLeadLost(
            command_id="cmd-x", correlation_id="c", lead_id=lead_id, expected_lead_version=lead(db, lead_id).version,
            reason=LostReason.NO_BUDGET))
    assert lead(db, lead_id).stage is LeadStage.OPPORTUNITY


def test_a_stale_terminal_command_is_rejected_without_writes(db: Database) -> None:
    lead_id = opportunity_lead(db)
    seen = lead(db, lead_id).version
    start_negotiation(db, lead_id)  # someone moved the lead meanwhile
    with pytest.raises(StaleCommandError) as error:
        ops(db).mark_lead_lost(AS_ALICE, MarkLeadLost(command_id="cmd-stale", correlation_id="c", lead_id=lead_id,
                                                      expected_lead_version=seen, reason=LostReason.NO_BUDGET))
    assert codes(error.value) == ["LEAD_VERSION_CHANGED"] and lead(db, lead_id).stage is LeadStage.NEGOTIATION


def test_terminal_commands_are_idempotent_and_collisions_are_refused(db: Database) -> None:
    lead_id = opportunity_lead(db)
    command = MarkLeadLost(command_id="cmd-lost-1", correlation_id="c", lead_id=lead_id,
                           expected_lead_version=lead(db, lead_id).version, reason=LostReason.NO_BUDGET)
    first = ops(db).mark_lead_lost(AS_ALICE, command)
    again = ops(db).mark_lead_lost(AS_ALICE, command.model_copy(update={"correlation_id": "retry"}))
    assert (first.replayed, again.replayed, again.outcome) == (False, True, first.outcome)
    with db.transaction() as uow:
        stops = uow.audit.list_by_event_type("LEAD_AUTOMATION_STOPPED", 10)
        transitions = uow.audit.list_by_event_type("PIPELINE_TRANSITION", 50)
    assert len(stops) == 1 and sum(1 for t in transitions if (t.after or {}).get("stage") == "CLOSED") == 1
    with pytest.raises(CommandCollisionError):
        ops(db).mark_lead_lost(AS_ALICE, command.model_copy(update={"reason": LostReason.TIMING}))


def test_disqualification_is_reasoned_and_distinct_from_not_qualified_yet(db: Database) -> None:
    not_yet = qualifying_lead(db, {"need": "Automate invoice matching"})
    assert status(db, not_yet) is QualificationStatus.IN_PROGRESS
    disqualify(db, not_yet, DisqualificationReason.OUTSIDE_SERVICE_AREA)
    q = qualification(db, not_yet)
    assert q is not None and (q.status, q.disqualification_reason) == (
        QualificationStatus.DISQUALIFIED, DisqualificationReason.OUTSIDE_SERVICE_AREA)
    assert (lead(db, not_yet).stage, lead(db, not_yet).close_reason) == (LeadStage.CLOSED, CloseReason.DISQUALIFIED)


def test_disqualifying_a_lead_with_an_active_opportunity_is_refused(db: Database) -> None:
    lead_id = opportunity_lead(db)
    with pytest.raises(CommandRejectedError) as error:
        disqualify(db, lead_id)
    assert codes(error.value) == ["OPPORTUNITY_ACTIVE"]  # an opportunity is closed by marking the lead LOST


# ---- Terminal automation effects -----------------------------------------------------------------


def test_lost_stops_campaign_automation_and_cancels_the_campaign_draft(db: Database) -> None:
    member_id = ready_campaign(db)
    scheduler(db).schedule(CAMPAIGN_ID, correlation_id="c")
    lead_id = member(db, member_id).lead_id or ""
    mark_lost(db, lead_id)
    with db.transaction() as uow:
        [job] = uow.campaign_jobs.list_for_member(member_id)
    assert job.status is CampaignJobStatus.CANCELLED and member(db, member_id).status is CampaignMemberStatus.CANCELLED


def test_lost_cancels_an_approved_but_undispatched_campaign_touch(db: Database) -> None:
    member_id = ready_campaign(db)
    drafted = draft_touch(db)
    assert drafted.outbound_id is not None
    approve_campaign_draft(db, drafted.outbound_id)
    lead_id = member(db, member_id).lead_id or ""
    mark_lost(db, lead_id)
    with db.transaction() as uow:
        message = uow.outbound.get(drafted.outbound_id)
    assert message is not None and message.status is OutboundStatus.CANCELLED
    assert member(db, member_id).status is CampaignMemberStatus.CANCELLED


def test_lost_cancels_the_follow_up_job_but_keeps_accepted_history(db: Database) -> None:
    replied = replied_conversation(db)
    follow_up_scheduler(db).schedule(replied.conversation_id, correlation_id="c")
    mark_lost(db, replied.lead_id)
    with db.transaction() as uow:
        [job] = uow.follow_up_jobs.list_for_conversation(replied.conversation_id)
        conversation = uow.conversations.get(replied.conversation_id)
        sent = uow.outbound.get(replied.reply_outbound_id)
    assert job.status is FollowUpJobStatus.CANCELLED
    assert conversation is not None and conversation.status is ConversationStatus.CLOSED
    assert sent is not None and sent.status is OutboundStatus.SENT  # accepted history is never touched


def test_quota_is_released_exactly_once(db: Database) -> None:
    result = process(db, happy_transport(), envelope("p-1"))
    lead_id, outbound_id = result.lead_id or "", result.outbound_id or ""
    with db.transaction() as uow:
        drafted = uow.outbound.get(outbound_id)
        assert drafted is not None
        approved = OutboundMessage.model_validate(drafted.model_dump() | {
            "status": OutboundStatus.APPROVED, "decision": OutboundDecision.SEND, "send_permit_id": "permit-1",
            "approved_at": NOW, "version": drafted.version + 1})
        uow.outbound.update(approved, drafted.version)
        reservation = reserve_quota(uow, policy.limits(sends=1, new_contacts=10), approved, reservation_id="res-1", now=NOW)
    command = MarkLeadLost(command_id="cmd-lost", correlation_id="c", lead_id=lead_id,
                           expected_lead_version=lead(db, lead_id).version, reason=LostReason.NO_BUDGET)
    first = ops(db).mark_lead_lost(AS_ALICE, command)
    assert ops(db).mark_lead_lost(AS_ALICE, command).replayed  # the replay changes nothing
    with db.transaction() as uow:
        released = uow.quota_reservations.get(reservation.reservation_id)
        message = uow.outbound.get(outbound_id)
    assert released is not None and released.state is QuotaReservationState.RELEASED and released.version == 2
    assert message is not None and message.status is OutboundStatus.CANCELLED
    assert f"CANCELLED:{outbound_id}" in first.outcome.reason_codes


def test_won_converts_the_conversation_and_cancels_undispatched_drafts(db: Database) -> None:
    lead_id = opportunity_lead(db)
    with db.transaction() as uow:
        drafts = [m for m in uow.outbound.list_by_lead(lead_id) if m.status is OutboundStatus.DRAFTED]
        [conversation] = uow.conversations.list_by_lead(lead_id)
    assert drafts  # the Stage 6 reply draft is still waiting for review
    mark_won(db, lead_id)
    with db.transaction() as uow:
        after = uow.conversations.get(conversation.conversation_id)
        statuses = {m.outbound_id: m.status for m in uow.outbound.list_by_lead(lead_id)}
    assert after is not None and after.status is ConversationStatus.CONVERTED
    assert all(statuses[d.outbound_id] is OutboundStatus.CANCELLED for d in drafts)


def test_won_does_not_imply_do_not_contact(db: Database) -> None:
    lead_id = opportunity_lead(db)
    mark_won(db, lead_id)
    view = pipeline(db).view(lead_id)
    assert not view.suppressed and view.next_action.action.value == "CLOSED"


# ---- Reopen --------------------------------------------------------------------------------------------


def test_reopen_is_operator_only_preserves_history_and_resurrects_nothing(db: Database) -> None:
    replied = replied_conversation(db)
    follow_up_scheduler(db).schedule(replied.conversation_id, correlation_id="c")
    mark_lost(db, replied.lead_id)
    reopen(db, replied.lead_id, LeadStage.QUALIFYING)
    reopened = lead(db, replied.lead_id)
    assert (reopened.stage, reopened.close_reason, reopened.status) == (LeadStage.QUALIFYING, None, LeadStatus.OPERATOR_OWNED)
    with db.transaction() as uow:
        [job] = uow.follow_up_jobs.list_for_conversation(replied.conversation_id)
        conversation = uow.conversations.get(replied.conversation_id)
        events = sorted(uow.audit.list_by_event_type("PIPELINE_TRANSITION", 50), key=lambda e: int(str((e.after or {})["version"])))
        moves = [((e.before or {}).get("stage"), (e.after or {}).get("stage"), (e.after or {}).get("trigger")) for e in events]
    assert job.status is FollowUpJobStatus.CANCELLED and conversation is not None and conversation.status is ConversationStatus.CLOSED
    assert moves[-2:] == [("INTERESTED", "CLOSED", "OPERATOR_MARKED_LOST"), ("CLOSED", "QUALIFYING", "OPERATOR_REOPENED")]
    assert follow_up_scheduler(db).schedule(replied.conversation_id, correlation_id="c2").follow_up_id is None


def test_won_and_suppression_closes_are_never_reopened(db: Database) -> None:
    lead_id = opportunity_lead(db)
    mark_won(db, lead_id)
    with pytest.raises(CommandRejectedError) as error:
        reopen(db, lead_id)
    assert codes(error.value) == ["NOT_REOPENABLE"]


def test_reopen_targets_are_configured_early_stages(db: Database) -> None:
    lead_id = qualified_lead(db)
    mark_lost(db, lead_id)
    with pytest.raises(CommandRejectedError) as error:
        reopen(db, lead_id, LeadStage.NEGOTIATION)
    assert codes(error.value) == ["REOPEN_TARGET_NOT_ALLOWED"]
    reopen(db, lead_id, LeadStage.QUALIFYING)
    assert status(db, lead_id) is QualificationStatus.READY_FOR_REVIEW  # facts kept, decision undone
