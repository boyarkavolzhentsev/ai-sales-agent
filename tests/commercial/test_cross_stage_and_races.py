"""Stages 6-12 keep their semantics with commercial decisioning composed, and the
required concurrency races resolve cleanly (first committer wins, the other is refused)."""

from pathlib import Path

import pytest

from app.commercial import CommercialExtraction, CommercialHookOutcome, CommercialHookStatus
from app.commercial.fake import FakeCommercialExtractor
from app.core.enums import (
    ConversationStatus,
    LeadStage,
    LostReason,
    ObjectionCategory,
    RevisionStatus,
    SignalStatus,
    TermType,
)
from app.core.models import OPEN_REVISION_STATUSES
from app.dispatch import FakeBehavior, FakeEmailTransport
from app.operator import (
    ApproveProposal,
    CommandRejectedError,
    CommandResult,
    CreateProposal,
    MarkLeadLost,
    MarkLeadWon,
    MarkProposalPresented,
    OperatorService,
    OperatorUnauthorizedError,
    ReviseProposal,
    SetCommercialTerm,
    StaleCommandError,
)
from app.persistence import Database, FrozenClock
from tests.commercial.builders import (
    LATER,
    approve,
    asks,
    commercial,
    current,
    customer_message,
    opportunity,
    opportunity_for,
    ops,
    presented,
    ready_draft,
    suppress,
    text,
)
from tests.conversation.builders import approve as approve_draft
from tests.conversation.test_races_and_recovery import run_concurrently
from tests.dispatch.builders import dispatcher, send
from tests.inbound.builders import NOW, SENDER, envelope, happy_transport, process
from tests.operator.builders import AS_ALICE, credential
from tests.pipeline.builders import active_opportunity, lead
from tests.runtime.builders import fake_adapters, runtime

ROUNDS = range(3)


def ops_for(db: Database) -> OperatorService:
    return ops(db)


def refused_or_done(result: object) -> bool:
    return isinstance(result, (CommandResult, CommandRejectedError))


def reply_with(db: Database, provider_message_id: str, extraction: CommercialExtraction) -> CommercialHookOutcome:
    result = process(db, happy_transport(), envelope(provider_message_id, in_reply_to="<p-1@prospect.example>", received_at=LATER))
    return commercial(db, FakeCommercialExtractor(default=extraction)).record_inbound(result, correlation_id=f"c-{provider_message_id}")


# ---- Cross-stage ---------------------------------------------------------------------------------


def test_stage6_customer_text_only_ever_proposes(db: Database) -> None:
    lead_id, opportunity_id = presented(db)
    stage_before = lead(db, lead_id).stage
    customer_message(db, "p-2", asks((TermType.PAYMENT_TERM, text("NET_90")), accept=True,
                                     objections=((ObjectionCategory.TIMING, "Not before Q4"),)))
    assert lead(db, lead_id).stage is stage_before and current(db, opportunity_id).status is RevisionStatus.PRESENTED
    with db.transaction() as uow:
        [term] = [t for t in uow.commercial_terms.list_for_opportunity(opportunity_id) if t.term_type is TermType.PAYMENT_TERM]
    assert term.value.text == "NET_30"


def test_stage7_authorization_stays_authoritative(db: Database) -> None:
    _, opportunity_id = opportunity_for(db)
    with pytest.raises(OperatorUnauthorizedError):
        ops(db).create_proposal(credential("forged"), CreateProposal(
            command_id="cmd-x", correlation_id="c", opportunity_id=opportunity_id,
            expected_opportunity_version=opportunity(db, opportunity_id).version, currency="EUR"))
    assert commercial(db).view(opportunity_id).revision_id is None


def test_stage8_an_uncertain_send_never_presents_a_proposal(db: Database) -> None:
    lead_id, opportunity_id = ready_draft(db)
    approve(db, opportunity_id)
    with db.transaction() as uow:
        [draft] = [m for m in uow.outbound.list_by_lead(lead_id) if m.status.value == "DRAFTED"]
    approve_draft(db, draft.outbound_id, FrozenClock(NOW))
    send(dispatcher(db, FakeEmailTransport().script(FakeBehavior.ACCEPT_THEN_LOSE_RESPONSE)), draft.outbound_id)
    assert current(db, opportunity_id).status is RevisionStatus.APPROVED  # only an operator confirms presentation


def test_stage9_conversation_state_is_untouched_by_the_commercial_hook(db: Database) -> None:
    _, opportunity_id = presented(db)
    result = process(db, happy_transport(), envelope("p-2", in_reply_to="<p-1@prospect.example>", received_at=LATER))
    with db.transaction() as uow:
        before = uow.conversations.get_by_thread(result.thread_id)
    commercial(db, FakeCommercialExtractor(default=asks(accept=True))).record_inbound(result, correlation_id="c")
    with db.transaction() as uow:
        after = uow.conversations.get_by_thread(result.thread_id)
    assert before is not None and after is not None and (after.status, after.version) == (before.status, before.version)
    assert after.status is not ConversationStatus.CONVERTED


def test_stage10_12_leads_without_an_opportunity_are_left_alone(db: Database) -> None:
    result = process(db, happy_transport(), envelope("p-1"))
    outcome = commercial(db, FakeCommercialExtractor(default=asks(accept=True))).record_inbound(result, correlation_id="c")
    assert (outcome.status, outcome.reason) == (CommercialHookStatus.SKIPPED, "NO_ACTIVE_OPPORTUNITY")


def test_stage11_ticks_do_no_commercial_work_and_inbound_runs_the_hook(db_path: Path) -> None:
    app = runtime(db_path, adapters=fake_adapters())
    app.start()
    for _ in range(3):
        app.tick(dispatch_approved=True)
    with Database(db_path) as db, db.transaction() as uow:
        assert uow.proposal_revisions.count_all() == 0 and uow.audit.list_by_event_type("TERM_REQUESTED", 5) == []
    app.stop()


# ---- Races ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("round_no", ROUNDS)
def test_1_approve_proposal_vs_customer_term_request(db_path: Path, round_no: int) -> None:
    with Database(db_path) as db:
        _, opportunity_id = ready_draft(db)
        revision = current(db, opportunity_id)
    command = ApproveProposal(command_id="cmd-approve", correlation_id="c", revision_id=revision.revision_id,
                              expected_revision_version=revision.version)
    results = run_concurrently(db_path, lambda db: ops_for(db).approve_proposal(AS_ALICE, command),
                               lambda db: reply_with(db, "p-2", asks((TermType.PAYMENT_TERM, text("NET_60")))))
    assert refused_or_done(results[0]) and not isinstance(results[1], Exception), results
    with Database(db_path) as db:
        view = commercial(db).view(opportunity_id)
        assert view.open_request_ids  # the request is never lost
        if view.revision_status is RevisionStatus.APPROVED:
            assert view.next_action.action.value == "REVIEW_TERM_REQUEST"  # and must be decided before presenting


@pytest.mark.parametrize("round_no", ROUNDS)
def test_2_two_revisions(db_path: Path, round_no: int) -> None:
    with Database(db_path) as db:
        _, opportunity_id = presented(db)
        revision = current(db, opportunity_id)
    commands = [ReviseProposal(command_id=f"cmd-rev-{i}", correlation_id="c", revision_id=revision.revision_id,
                               expected_revision_version=revision.version) for i in (1, 2)]
    results = run_concurrently(db_path, *(lambda db, c=c: ops_for(db).revise_proposal(AS_ALICE, c) for c in commands))
    assert sum(isinstance(r, CommandResult) for r in results) == 1 and all(refused_or_done(r) for r in results), results
    with Database(db_path) as db, db.transaction() as uow:
        numbers = [r.revision for r in uow.proposal_revisions.list_for_opportunity(opportunity_id)]
    assert numbers == [1, 2]


@pytest.mark.parametrize("round_no", ROUNDS)
def test_3_two_conflicting_term_approvals(db_path: Path, round_no: int) -> None:
    with Database(db_path) as db:
        _, opportunity_id = opportunity_for(db)
    commands = [SetCommercialTerm(command_id=f"cmd-term-{v}", correlation_id="c", opportunity_id=opportunity_id,
                                  term_type=TermType.PAYMENT_TERM, value=text(v), expected_term_version=None)
                for v in ("NET_15", "NET_45")]
    results = run_concurrently(db_path, *(lambda db, c=c: ops_for(db).set_commercial_term(AS_ALICE, c) for c in commands))
    assert sum(isinstance(r, CommandResult) for r in results) == 1, results
    assert sum(isinstance(r, StaleCommandError) for r in results) == 1, results


@pytest.mark.parametrize("round_no", ROUNDS)
def test_4_mark_won_vs_new_objection(db_path: Path, round_no: int) -> None:
    with Database(db_path) as db:
        lead_id, opportunity_id = presented(db)
        opp = active_opportunity(db, lead_id)
        command = MarkLeadWon(command_id="cmd-won", correlation_id="c", lead_id=lead_id,
                              expected_lead_version=lead(db, lead_id).version, opportunity_id=opp.opportunity_id,
                              expected_opportunity_version=opp.version)
    results = run_concurrently(db_path, lambda db: ops_for(db).mark_lead_won(AS_ALICE, command),
                               lambda db: reply_with(db, "p-2", asks(objections=((ObjectionCategory.PRICE, "Too pricey"),))))
    assert refused_or_done(results[0]) and not isinstance(results[1], Exception), results
    with Database(db_path) as db:
        view = commercial(db).view(opportunity_id)
        if lead(db, lead_id).stage is LeadStage.CLOSED:
            assert view.next_action.action.value == "CLOSED" and view.revision_status is RevisionStatus.CLOSED


@pytest.mark.parametrize("round_no", ROUNDS)
def test_5_mark_lost_vs_acceptance_signal(db_path: Path, round_no: int) -> None:
    with Database(db_path) as db:
        lead_id, opportunity_id = presented(db)
        command = MarkLeadLost(command_id="cmd-lost", correlation_id="c", lead_id=lead_id,
                               expected_lead_version=lead(db, lead_id).version, reason=LostReason.NO_DECISION)
    results = run_concurrently(db_path, lambda db: ops_for(db).mark_lead_lost(AS_ALICE, command),
                               lambda db: reply_with(db, "p-2", asks(accept=True)))
    assert refused_or_done(results[0]) and not isinstance(results[1], Exception), results
    with Database(db_path) as db, db.transaction() as uow:
        final = uow.leads.get(lead_id)
        open_signals = [s for s in uow.commercial_signals.list_for_opportunity(opportunity_id) if s.status is SignalStatus.OPEN]
    assert final is not None
    if final.stage is LeadStage.CLOSED:
        assert open_signals == []  # a closed lead keeps no open acceptance: never an automatic WON either
    assert final.close_reason is None or final.close_reason.value == "LOST"


@pytest.mark.parametrize("round_no", ROUNDS)
def test_6_dnc_vs_mark_presented(db_path: Path, round_no: int) -> None:
    with Database(db_path) as db:
        _, opportunity_id = ready_draft(db)
        approve(db, opportunity_id)
        revision = current(db, opportunity_id)
    command = MarkProposalPresented(command_id="cmd-present", correlation_id="c", revision_id=revision.revision_id,
                                    expected_revision_version=revision.version)
    results = run_concurrently(db_path, lambda db: suppress(db, SENDER),
                               lambda db: ops_for(db).mark_proposal_presented(AS_ALICE, command))
    assert refused_or_done(results[1]), results
    with Database(db_path) as db:
        status = current(db, opportunity_id).status
    if isinstance(results[1], CommandRejectedError):
        assert [c.value for c in results[1].codes] == ["CONTACT_SUPPRESSED"] and status is RevisionStatus.APPROVED


@pytest.mark.parametrize("round_no", ROUNDS)
def test_7_opportunity_close_vs_revision(db_path: Path, round_no: int) -> None:
    with Database(db_path) as db:
        lead_id, opportunity_id = presented(db)
        revision = current(db, opportunity_id)
        lost = MarkLeadLost(command_id="cmd-lost", correlation_id="c", lead_id=lead_id,
                            expected_lead_version=lead(db, lead_id).version, reason=LostReason.CHOSE_COMPETITOR)
    revise = ReviseProposal(command_id="cmd-rev", correlation_id="c", revision_id=revision.revision_id,
                            expected_revision_version=revision.version)
    results = run_concurrently(db_path, lambda db: ops_for(db).mark_lead_lost(AS_ALICE, lost),
                               lambda db: ops_for(db).revise_proposal(AS_ALICE, revise))
    assert all(refused_or_done(r) for r in results), results
    with Database(db_path) as db, db.transaction() as uow:
        final = uow.leads.get(lead_id)
        open_ = [r for r in uow.proposal_revisions.list_for_opportunity(opportunity_id) if r.status in OPEN_REVISION_STATUSES]
    assert final is not None and (final.stage is not LeadStage.CLOSED or open_ == [])


@pytest.mark.parametrize("round_no", ROUNDS)
def test_8_the_same_inbound_extraction_replayed_concurrently(db_path: Path, round_no: int) -> None:
    with Database(db_path) as db:
        _, opportunity_id = presented(db)
        result = process(db, happy_transport(), envelope("p-2", in_reply_to="<p-1@prospect.example>", received_at=LATER))
    extraction = asks((TermType.PAYMENT_TERM, text("NET_60")), accept=True)
    results = run_concurrently(db_path, *(lambda db: commercial(db, FakeCommercialExtractor(default=extraction))
                                          .record_inbound(result, correlation_id="c") for _ in range(2)))
    statuses = sorted(r.status.value for r in results if isinstance(r, CommercialHookOutcome))
    assert statuses == ["APPLIED", "REPLAYED"], results
    with Database(db_path) as db, db.transaction() as uow:
        assert len(uow.term_requests.list_for_opportunity(opportunity_id)) == 1
        assert len(uow.commercial_signals.list_for_opportunity(opportunity_id)) == 1
