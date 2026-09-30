"""Concurrent races on separate connections. Every command is built from one shared
snapshot before the race; whichever side commits first wins and the other is refused as
stale or not allowed. No unexpected exception escapes and no invariant breaks."""

from pathlib import Path

import pytest

from app.core.enums import (
    CloseReason,
    DNCReason,
    DNCScope,
    LeadIntent,
    LeadStage,
    LostReason,
    OpportunityStatus,
    OutboundStatus,
    QualificationStatus,
    RefKind,
)
from app.core.models import DoNotContactEntry, EntityRef
from app.inbound import InboundResult
from app.llm import LLMTask
from app.operator import (
    ApproveQualification,
    CommandRejectedError,
    CommandResult,
    CreateOpportunity,
    MarkLeadLost,
    MarkLeadWon,
    OperatorService,
    ReopenLead,
)
from app.persistence import Database, FrozenClock
from app.pipeline.fake import FakeQualificationExtractor
from app.pipeline.next_action import IN_SEQUENCE
from tests.campaign.builders import CAMPAIGN_ID, claim_all, executor as campaign_executor, member, ready_campaign, scheduler
from tests.conversation.builders import FIRST_DUE, claim_one, executor as follow_up_executor, replied_conversation
from tests.conversation.builders import scheduler as follow_up_scheduler
from tests.conversation.test_races_and_recovery import run_concurrently
from tests.inbound.builders import NOW, SENDER, ScriptedTransport, classification, envelope, process
from tests.operator.builders import AS_ALICE, operator
from tests.pipeline.builders import (
    REQUIRED,
    active_opportunity,
    extraction,
    lead,
    mark_lost,
    opportunity_lead,
    pipeline,
    qualification,
    qualified_lead,
    qualifying_lead,
)

ROUNDS = range(3)


def ops_for(db: Database) -> OperatorService:
    """An operator service on this thread's own connection."""
    return operator(db, FrozenClock(NOW))


def suppress(db: Database) -> str:
    with db.transaction() as uow:
        uow.dnc.add(DoNotContactEntry(entry_id="dnc-race", scope=DNCScope.EMAIL, value=SENDER, reason=DNCReason.OPERATOR,
                                      source_ref=EntityRef(kind=RefKind.OPERATOR_COMMAND, id="c"), created_by="op",
                                      created_at=NOW))
    return "SUPPRESSED"


def refused_or_done(result: object) -> bool:
    return isinstance(result, (CommandResult, CommandRejectedError))


def reply(db: Database, provider_message_id: str, intent: LeadIntent = LeadIntent.NEGOTIATION,
          extractor: FakeQualificationExtractor | None = None) -> InboundResult:
    result = process(db, ScriptedTransport().script(LLMTask.INTENT_CLASSIFICATION, classification(intent)),
                     envelope(provider_message_id, body="One more thing.", in_reply_to="<p-1@prospect.example>"))
    pipeline(db, extractor).record_inbound(result, correlation_id=f"corr-{provider_message_id}")
    return result


@pytest.mark.parametrize("round_no", ROUNDS)
def test_a_customer_reply_vs_operator_lost(db_path: Path, round_no: int) -> None:
    with Database(db_path) as db:
        lead_id = qualified_lead(db)
        command = MarkLeadLost(command_id="cmd-lost", correlation_id="c", lead_id=lead_id,
                               expected_lead_version=lead(db, lead_id).version, reason=LostReason.NO_DECISION)
    results = run_concurrently(db_path, lambda db: reply(db, "p-2"), lambda db: ops_for(db).mark_lead_lost(AS_ALICE, command))
    assert isinstance(results[0], InboundResult), results  # the reply is always observed, never lost
    assert refused_or_done(results[1]), results
    with Database(db_path) as db, db.transaction() as uow:
        assert uow.messages.get(results[0].message_id) is not None
        final = uow.leads.get(lead_id)
        assert final is not None
        if final.stage is LeadStage.CLOSED:  # LOST won the race: nothing undispatched survives for it
            assert all(m.status not in (OutboundStatus.DRAFTED, OutboundStatus.OPERATOR_APPROVED)
                       for m in uow.outbound.list_by_lead(lead_id))


@pytest.mark.parametrize("round_no", ROUNDS)
def test_b_dnc_vs_won(db_path: Path, round_no: int) -> None:
    with Database(db_path) as db:
        lead_id = opportunity_lead(db)
        opp = active_opportunity(db, lead_id)
        command = MarkLeadWon(command_id="cmd-won", correlation_id="c", lead_id=lead_id,
                              expected_lead_version=lead(db, lead_id).version, opportunity_id=opp.opportunity_id,
                              expected_opportunity_version=opp.version)
    results = run_concurrently(db_path, suppress, lambda db: ops_for(db).mark_lead_won(AS_ALICE, command))
    assert results[0] == "SUPPRESSED" and refused_or_done(results[1]), results
    with Database(db_path) as db:
        final = lead(db, lead_id)
        if isinstance(results[1], CommandRejectedError):
            assert [c.value for c in results[1].codes] == ["CONTACT_SUPPRESSED"] and final.stage is LeadStage.OPPORTUNITY
        else:
            assert final.close_reason is CloseReason.WON  # won before the suppression existed
        assert pipeline(db).view(lead_id).next_action.owner.value == "NONE"  # either way nothing progresses


@pytest.mark.parametrize("round_no", ROUNDS)
def test_c_qualification_approval_vs_a_conflicting_fact(db_path: Path, round_no: int) -> None:
    with Database(db_path) as db:
        lead_id = qualifying_lead(db, REQUIRED | {"budget": "50k EUR"})
        q = qualification(db, lead_id)
        assert q is not None
        command = ApproveQualification(command_id="cmd-approve", correlation_id="c", lead_id=lead_id,
                                       expected_lead_version=lead(db, lead_id).version, expected_qualification_version=q.version)
    conflicting = FakeQualificationExtractor(default=extraction(budget="20k EUR"))
    results = run_concurrently(db_path, lambda db: ops_for(db).approve_qualification(AS_ALICE, command),
                               lambda db: reply(db, "p-2", LeadIntent.INFO_REQUEST, conflicting))
    assert refused_or_done(results[0]) and isinstance(results[1], InboundResult), results
    with Database(db_path) as db:
        final = qualification(db, lead_id)
        assert final is not None and final.fact("budget") is not None
        assert final.fact("budget").value == "50k EUR"  # never overwritten silently
        assert len(final.open_conflicts) == 1  # surfaced in both orders
        if isinstance(results[0], CommandRejectedError):
            assert final.status is QualificationStatus.IN_PROGRESS  # the operator must re-read and resolve first


@pytest.mark.parametrize("round_no", ROUNDS)
def test_d_two_opportunity_creates(db_path: Path, round_no: int) -> None:
    with Database(db_path) as db:
        lead_id = qualified_lead(db)
        version = lead(db, lead_id).version
    commands = [CreateOpportunity(command_id=f"cmd-opp-{i}", correlation_id="c", lead_id=lead_id,
                                  expected_lead_version=version) for i in (1, 2)]
    results = run_concurrently(db_path, *(lambda db, c=c: ops_for(db).create_opportunity(AS_ALICE, c) for c in commands))
    assert sorted(type(r).__name__ for r in results) in (["CommandResult", "StaleCommandError"],
                                                          ["CommandRejectedError", "CommandResult"]), results
    with Database(db_path) as db, db.transaction() as uow:
        assert [o.status for o in uow.opportunities.list_by_lead(lead_id)] == [OpportunityStatus.OPEN]


@pytest.mark.parametrize("round_no", ROUNDS)
def test_e_terminal_transition_vs_campaign_executor(db_path: Path, round_no: int) -> None:
    with Database(db_path) as db:
        member_id = ready_campaign(db)
        lead_id = member(db, member_id).lead_id or ""
        scheduler(db).schedule(CAMPAIGN_ID, correlation_id="c")
        [claim] = claim_all(db, FrozenClock(NOW))
        command = MarkLeadLost(command_id="cmd-lost", correlation_id="c", lead_id=lead_id,
                               expected_lead_version=lead(db, lead_id).version, reason=LostReason.NO_RESPONSE)
    results = run_concurrently(db_path, lambda db: campaign_executor(db).execute(claim, correlation_id="c-exec"),
                               lambda db: ops_for(db).mark_lead_lost(AS_ALICE, command))
    assert not any(isinstance(r, Exception) and not isinstance(r, CommandRejectedError) for r in results), results
    with Database(db_path) as db, db.transaction() as uow:
        final = uow.leads.get(lead_id)
        assert final is not None
        if final.stage is LeadStage.CLOSED:
            assert all(m.status is not OutboundStatus.DRAFTED for m in uow.outbound.list_by_lead(lead_id))
            current = uow.campaign_members.get(member_id)
            assert current is not None and current.status not in IN_SEQUENCE


@pytest.mark.parametrize("round_no", ROUNDS)
def test_f_terminal_transition_vs_follow_up_executor(db_path: Path, round_no: int) -> None:
    with Database(db_path) as db:
        replied = replied_conversation(db)
        clock = FrozenClock(FIRST_DUE)
        follow_up_scheduler(db, clock).schedule(replied.conversation_id, correlation_id="c")
        claim = claim_one(db, clock)
        command = MarkLeadLost(command_id="cmd-lost", correlation_id="c", lead_id=replied.lead_id,
                               expected_lead_version=lead(db, replied.lead_id).version, reason=LostReason.NO_DECISION)
    results = run_concurrently(db_path, lambda db: follow_up_executor(db, FrozenClock(FIRST_DUE)).execute(claim, correlation_id="c-exec"),
                               lambda db: ops_for(db).mark_lead_lost(AS_ALICE, command))
    assert not any(isinstance(r, Exception) and not isinstance(r, CommandRejectedError) for r in results), results
    with Database(db_path) as db, db.transaction() as uow:
        final = uow.leads.get(replied.lead_id)
        assert final is not None
        if final.stage is LeadStage.CLOSED:
            assert all(m.status is not OutboundStatus.DRAFTED for m in uow.outbound.list_by_lead(replied.lead_id))
            assert uow.follow_up_jobs.get_open_for_conversation(replied.conversation_id) is None


@pytest.mark.parametrize("round_no", ROUNDS)
def test_g_reopen_vs_dnc(db_path: Path, round_no: int) -> None:
    with Database(db_path) as db:
        lead_id = qualified_lead(db)
        mark_lost(db, lead_id)
        command = ReopenLead(command_id="cmd-reopen", correlation_id="c", lead_id=lead_id,
                             expected_lead_version=lead(db, lead_id).version, target_stage=LeadStage.ENGAGED, note="Back.")
    results = run_concurrently(db_path, suppress, lambda db: ops_for(db).reopen_lead(AS_ALICE, command))
    assert results[0] == "SUPPRESSED" and refused_or_done(results[1]), results
    with Database(db_path) as db:
        view = pipeline(db).view(lead_id)
        assert view.suppressed and view.next_action.owner.value == "NONE"  # suppression stays, nothing resumes
        with db.transaction() as uow:
            assert len(uow.dnc.list_active(DNCScope.EMAIL, SENDER, NOW)) == 1


@pytest.mark.parametrize("round_no", ROUNDS)
def test_h_two_won_commands(db_path: Path, round_no: int) -> None:
    with Database(db_path) as db:
        lead_id = opportunity_lead(db)
        opp = active_opportunity(db, lead_id)
        version = lead(db, lead_id).version
    commands = [MarkLeadWon(command_id=f"cmd-won-{i}", correlation_id="c", lead_id=lead_id, expected_lead_version=version,
                            opportunity_id=opp.opportunity_id, expected_opportunity_version=opp.version) for i in (1, 2)]
    results = run_concurrently(db_path, *(lambda db, c=c: ops_for(db).mark_lead_won(AS_ALICE, c) for c in commands))
    assert sum(isinstance(r, CommandResult) for r in results) == 1, results
    assert sum(isinstance(r, CommandRejectedError) for r in results) == 1, results
    with Database(db_path) as db, db.transaction() as uow:
        assert len(uow.audit.list_by_event_type("LEAD_AUTOMATION_STOPPED", 10)) == 1
        won = [e for e in uow.audit.list_by_event_type("PIPELINE_TRANSITION", 50)
               if (e.after or {}).get("trigger") == "OPERATOR_MARKED_WON"]
        final = uow.leads.get(lead_id)
    assert len(won) == 1 and final is not None and final.close_reason is CloseReason.WON
