"""Concurrency races around the coordinator (threads, separate SQLite connections). Each
race may resolve either way; the invariants must hold both ways and no unhandled
concurrency exception may leak."""

from datetime import timedelta
from pathlib import Path

import pytest

from app.core.enums import CampaignMemberStatus, LeadStage, LostReason, OutboundStatus, RevisionStatus, SignalStatus
from app.dispatch import FakeBehavior, FakeEmailTransport
from app.operator import (
    ApproveProposal,
    ApproveQualification,
    CommandRejectedError,
    CommandResult,
    MarkLeadLost,
    StaleCommandError,
    UpdateProposal,
)
from app.orchestration import ExecutionAction as A
from app.orchestration import ExecutionOutcome as X
from app.orchestration import ExecutionResult
from app.persistence import Database, FrozenClock
from tests.campaign.builders import PROSPECT, campaign_messages
from tests.commercial.builders import LATER, asks, current, line, presented, ready_draft
from tests.conversation.test_races_and_recovery import run_concurrently
from tests.dispatch.builders import dispatcher
from tests.inbound.builders import envelope, happy_transport, service
from tests.operator.builders import AS_ALICE
from tests.orchestration.builders import (
    approve_pending,
    enrolled,
    orchestrator,
    quiet_message,
    reject_pending,
    suppress,
    world,
)
from tests.pipeline.builders import inbound, lead, lost_command, ops, qualification, qualifying_lead

ROUNDS = range(3)
REFUSED = (StaleCommandError, CommandRejectedError)


def clean(*results: object) -> None:
    """Every racer returned a result or a domain refusal: nothing else leaked."""
    for result in results:
        assert not isinstance(result, Exception) or isinstance(result, REFUSED), repr(result)
        assert not (isinstance(result, ExecutionResult) and result.outcome is X.ERROR), result


# ---- A. campaign action vs customer reply ------------------------------------------------------


@pytest.mark.parametrize("round_", ROUNDS)
def test_a_campaign_touch_vs_customer_reply(db_path: Path, round_: int) -> None:
    w = world(db_path)
    enrolled(w)
    w.execute()
    approve_pending(w)
    w.execute(dispatch=True)
    w.advance(timedelta(days=3))  # the second touch is due
    plan = w.plan()
    assert (plan.action, plan.executable) == (A.PREPARE_CAMPAIGN_TOUCH, True)
    [sent] = campaign_messages(w.db, w.lead)
    at = w.clock.now()
    reply = envelope("p-race", sender=PROSPECT, body="How much is the Basic plan per month?", in_reply_to=sent.rfc_message_id,
                     received_at=at)
    results = run_concurrently(
        db_path,
        lambda db: orchestrator(db, at=at).execute(w.lead, plan.fingerprint, correlation_id="race-a"),
        lambda db: service(db, happy_transport(), clock=FrozenClock(at)).process(reply, correlation_id="race-a-reply"),
    )
    clean(*results)
    with w.db.transaction() as uow:
        member = uow.campaign_members.get(w.member_id or "")
    assert member is not None and member.status is CampaignMemberStatus.REPLIED
    second_touch = [m for m in campaign_messages(w.db, w.lead) if m.outbound_id != sent.outbound_id]
    assert all(m.status is OutboundStatus.CANCELLED for m in second_touch)  # no new campaign outbound survives
    assert w.plan().owner.value != "CAMPAIGN" and len(w.transport.calls) == 1
    w.app.stop()


# ---- B. send plan vs DNC ---------------------------------------------------------------------------


@pytest.mark.parametrize("round_", ROUNDS)
def test_b_send_plan_vs_dnc(db_path: Path, round_: int) -> None:
    w = world(db_path)
    enrolled(w)
    w.execute()
    approve_pending(w)
    plan = w.plan()
    assert plan.action is A.SEND_APPROVED_MESSAGE

    def add_dnc(db: Database) -> None:
        from tests.commercial.builders import suppress as add
        add(db, PROSPECT)

    results = run_concurrently(
        db_path,
        lambda db: orchestrator(db, transport=w.transport).execute(w.lead, plan.fingerprint, correlation_id="race-b",
                                                                   allow_dispatch=True),
        add_dnc,
    )
    clean(*results)
    executed = results[0]
    assert isinstance(executed, ExecutionResult)
    assert len(w.transport.calls) <= 1
    if not w.transport.calls:
        assert executed.outcome in (X.STALE_PLAN, X.BLOCKED)
    assert w.plan().action is A.NO_AUTOMATION
    w.app.stop()


def test_b_dnc_landing_after_the_fingerprint_check_is_refused_by_stage8(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The window between the coordinator's re-plan and Stage 8's claim: Stage 8 revalidates
    and fails closed."""
    w = world(db_path)
    enrolled(w)
    w.execute()
    approve_pending(w)
    coordinator = orchestrator(w.db, transport=w.transport)
    believed = coordinator.plan(w.lead)
    suppress(w)
    monkeypatch.setattr(coordinator, "plan", lambda lead_id: believed)  # the coordinator still sees the old state
    result = coordinator.execute(w.lead, believed.fingerprint, correlation_id="race-b2", allow_dispatch=True)
    assert (result.outcome, result.reason) == (X.BLOCKED, "STAGE8_REFUSED") and w.transport.calls == []
    w.app.stop()


# ---- C. operator review plan vs new customer message -------------------------------------------


@pytest.mark.parametrize("round_", ROUNDS)
def test_c_operator_review_vs_new_inbound(db_path: Path, round_: int) -> None:
    with Database(db_path) as db:
        lead_id = qualifying_lead(db)
        reject_pending(db)
        plan = orchestrator(db).plan(lead_id)
        assert plan.action is A.REVIEW_QUALIFICATION
        q = qualification(db, lead_id)
        assert q is not None
        command = ApproveQualification(command_id="cmd-race-q", correlation_id="c", lead_id=lead_id,
                                       expected_lead_version=lead(db, lead_id).version, expected_qualification_version=q.version)
    results = run_concurrently(
        db_path,
        lambda db: ops(db).approve_qualification(AS_ALICE, command),
        lambda db: inbound(db, "p-2", facts={"timeframe": "Q1 2027"}, in_reply_to="<p-1@prospect.example>", received_at=LATER),
    )
    clean(*results)
    with Database(db_path) as db:
        coordinator = orchestrator(db, at=LATER)
        assert coordinator.plan(lead_id).fingerprint != plan.fingerprint  # the old snapshot is stale either way
        assert coordinator.execute(lead_id, plan.fingerprint, correlation_id="race-c").outcome is X.STALE_PLAN
        if isinstance(results[0], CommandResult):
            assert lead(db, lead_id).stage is LeadStage.QUALIFIED  # approved before the message arrived
        else:
            assert lead(db, lead_id).stage is LeadStage.QUALIFYING  # no approval against the stale snapshot


# ---- D. follow-up vs terminal close ------------------------------------------------------------


@pytest.mark.parametrize("round_", ROUNDS)
def test_d_follow_up_vs_lead_closed(db_path: Path, round_: int) -> None:
    w = world(db_path, facts={})
    result = w.app.handle_inbound(envelope("p-1"), correlation_id="corr-in")
    w.lead_id = result.lead_id
    approve_pending(w)
    w.execute(dispatch=True)
    assert w.execute().subsystem_outcome == "SCHEDULED"
    w.clock.set(w.plan().waiting_until + timedelta(minutes=1))  # type: ignore[operator]
    plan = w.plan()
    assert (plan.action, plan.executable) == (A.PROCESS_FOLLOW_UP, True)
    at = w.clock.now()
    lost = MarkLeadLost(command_id="cmd-race-lost", correlation_id="c", lead_id=w.lead,
                        expected_lead_version=w.lead_row().version, reason="NO_RESPONSE")  # type: ignore[arg-type]
    from tests.operator.builders import operator
    results = run_concurrently(
        db_path,
        lambda db: orchestrator(db, at=at).execute(w.lead, plan.fingerprint, correlation_id="race-d"),
        lambda db: operator(db, FrozenClock(at)).mark_lead_lost(AS_ALICE, lost),
    )
    clean(*results)
    assert w.lead_row().stage is LeadStage.CLOSED
    live = [m for m in w.messages() if m.status in (OutboundStatus.DRAFTED, OutboundStatus.PENDING_REVIEW,
                                                     OutboundStatus.OPERATOR_APPROVED, OutboundStatus.APPROVED)]
    assert live == []  # no follow-up survives the terminal close
    assert len(w.transport.calls) == 1 and w.plan().action is A.NO_ACTION
    w.app.stop()


# ---- E. acceptance signal vs mark LOST -------------------------------------------------------------


@pytest.mark.parametrize("round_", ROUNDS)
def test_e_acceptance_signal_vs_mark_lost(db_path: Path, round_: int) -> None:
    with Database(db_path) as db:
        lead_id, opportunity_id = presented(db)
        reject_pending(db)
        command = lost_command(db, lead_id, LostReason.CHOSE_COMPETITOR, "cmd-race-e")
    results = run_concurrently(
        db_path,
        lambda db: quiet_message(db, "p-yes", asks(accept=True), at=LATER),
        lambda db: ops(db).mark_lead_lost(AS_ALICE, command),
    )
    clean(*results)
    with Database(db_path) as db:
        final = lead(db, lead_id)
        revision = current(db, opportunity_id)
        with db.transaction() as uow:
            signals = uow.commercial_signals.list_for_opportunity(opportunity_id)
        plan = orchestrator(db, at=LATER).plan(lead_id)
    assert revision.status is not RevisionStatus.ACCEPTED  # nobody accepted on the customer's behalf
    if isinstance(results[1], CommandResult):  # LOST committed first: the late signal cannot contradict it
        assert final.stage is LeadStage.CLOSED and plan.action is A.NO_ACTION
        assert all(s.status is not SignalStatus.OPEN for s in signals)
    else:  # the acceptance committed first: the LOST prepared before it is stale, never applied
        assert isinstance(results[1], StaleCommandError) and "OPPORTUNITY_VERSION_CHANGED" in [c.value for c in results[1].codes]
        assert final.stage is not LeadStage.CLOSED and plan.action is A.CONFIRM_ACCEPTANCE


# ---- F. dispatch reconciliation vs retry work ------------------------------------------------------


@pytest.mark.parametrize("round_", ROUNDS)
def test_f_reconciliation_vs_retry(db_path: Path, round_: int) -> None:
    transport = FakeEmailTransport().script(FakeBehavior.ACCEPT_THEN_LOSE_RESPONSE)
    w = world(db_path, transport=transport)
    enrolled(w)
    w.execute()
    approve_pending(w)
    [outbound_id] = [m.outbound_id for m in campaign_messages(w.db, w.lead)]
    assert w.execute(dispatch=True).subsystem_outcome == "UNKNOWN"
    plan = w.plan()
    assert plan.action is A.RECONCILE_DISPATCH
    from tests.dispatch.builders import send
    results = run_concurrently(
        db_path,
        lambda db: orchestrator(db, transport=transport).execute(w.lead, plan.fingerprint, correlation_id="race-f"),
        lambda db: send(dispatcher(db, transport), outbound_id, "race-f-retry"),
    )
    clean(*results)
    assert len(transport.calls) == 1  # never resubmitted
    assert campaign_messages(w.db, w.lead)[0].status in (OutboundStatus.SENT, OutboundStatus.SENDING)
    w.app.stop()


# ---- G. commercial revision vs operator approval -------------------------------------------------


@pytest.mark.parametrize("round_", ROUNDS)
def test_g_revision_change_vs_proposal_approval(db_path: Path, round_: int) -> None:
    with Database(db_path) as db:
        lead_id, opportunity_id = ready_draft(db)
        reject_pending(db)
        plan = orchestrator(db).plan(lead_id)
        assert plan.action is A.REVIEW_PROPOSAL
        draft = current(db, opportunity_id)
    approve = ApproveProposal(command_id="cmd-race-g1", correlation_id="c", revision_id=draft.revision_id,
                              expected_revision_version=draft.version)
    change = UpdateProposal(command_id="cmd-race-g2", correlation_id="c", revision_id=draft.revision_id,
                            expected_revision_version=draft.version, lines=(line(quantity="24"),))
    results = run_concurrently(
        db_path,
        lambda db: ops(db).approve_proposal(AS_ALICE, approve),
        lambda db: ops(db).update_proposal(AS_ALICE, change),
    )
    clean(*results)
    assert sum(isinstance(r, CommandResult) for r in results) == 1  # first committer wins, the other is refused
    with Database(db_path) as db:
        coordinator = orchestrator(db)
        assert coordinator.execute(lead_id, plan.fingerprint, correlation_id="race-g").outcome is X.STALE_PLAN
        revision = current(db, opportunity_id)
    if isinstance(results[0], CommandResult):
        assert revision.status is RevisionStatus.APPROVED and [str(x.quantity) for x in revision.lines] == ["12"]
    else:
        assert revision.status is RevisionStatus.DRAFT and [str(x.quantity) for x in revision.lines] == ["24"]


# ---- H. two execution calls with the same fingerprint ----------------------------------------------


@pytest.mark.parametrize("round_", ROUNDS)
def test_h_two_executions_of_the_same_plan(db_path: Path, round_: int) -> None:
    w = world(db_path)
    enrolled(w)
    plan = w.plan()
    results = run_concurrently(
        db_path,
        lambda db: orchestrator(db).execute(w.lead, plan.fingerprint, correlation_id="race-h1"),
        lambda db: orchestrator(db).execute(w.lead, plan.fingerprint, correlation_id="race-h2"),
    )
    clean(*results)
    outcomes = sorted(r.outcome.value for r in results if isinstance(r, ExecutionResult))
    assert len(outcomes) == 2 and outcomes.count(X.EXECUTED.value) >= 1
    assert len(campaign_messages(w.db, w.lead)) == 1  # one draft, whatever the interleaving
    executed = [r for r in results if isinstance(r, ExecutionResult) and r.subsystem_outcome == "DRAFT_CREATED"]
    assert len(executed) == 1
    w.app.stop()


def test_e_acceptance_first_rejects_the_stale_lost(db: Database) -> None:
    """Race E made deterministic in the ordering the threads rarely produce: the customer's
    acceptance commits before an operator's LOST prepared on the older snapshot."""
    lead_id, opportunity_id = presented(db)
    reject_pending(db)
    before = orchestrator(db).plan(lead_id)
    stale = lost_command(db, lead_id, LostReason.CHOSE_COMPETITOR, "cmd-e-stale")
    quiet_message(db, "p-yes", asks(accept=True), at=LATER)
    coordinator = orchestrator(db, at=LATER)
    assert coordinator.execute(lead_id, before.fingerprint, correlation_id="e-seq").outcome is X.STALE_PLAN
    with pytest.raises(StaleCommandError):
        ops(db).mark_lead_lost(AS_ALICE, stale)
    assert lead(db, lead_id).stage is not LeadStage.CLOSED
    assert coordinator.plan(lead_id).action is A.CONFIRM_ACCEPTANCE  # the operator sees the newer evidence
    ops(db).mark_lead_lost(AS_ALICE, lost_command(db, lead_id, LostReason.CHOSE_COMPETITOR, "cmd-e-fresh"))  # fresh intent
    with db.transaction() as uow:
        signals = uow.commercial_signals.list_for_opportunity(opportunity_id)
    assert lead(db, lead_id).stage is LeadStage.CLOSED and [x.status for x in signals] == [SignalStatus.CANCELLED]
    assert current(db, opportunity_id).status is RevisionStatus.CLOSED and coordinator.plan(lead_id).action is A.NO_ACTION
