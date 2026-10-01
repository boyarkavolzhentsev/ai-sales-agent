"""The explicit executor: at most one action per call, stale plans refused, gated actions
reported (never performed), capabilities and the kill switch explicit, replays safe."""

from datetime import timedelta
from pathlib import Path

import pytest

from app.core.enums import CampaignJobStatus, CampaignMemberStatus, OutboundStatus
from app.orchestration import ExecutionAction as A
from app.orchestration import ExecutionBlocker as B
from app.orchestration import ExecutionOutcome as X
from app.orchestration import OrchestrationNotFoundError
from app.persistence import ConcurrencyError, Database
from app.policy import KillSwitchState
from tests.campaign.builders import campaign_messages
from tests.inbound.builders import NOW
from tests.orchestration.builders import (
    OFFLINE,
    approve_pending,
    enrolled,
    orchestrator,
    suppress,
    table_rows,
    world,
)


def test_one_call_runs_exactly_one_action_and_never_approves_or_sends(db_path: Path) -> None:
    w = world(db_path)
    enrolled(w)
    result = w.execute(dispatch=True)  # dispatch allowed, yet only the planned draft step runs
    assert (result.outcome, result.planned_action, result.subsystem_outcome) == (X.EXECUTED, A.PREPARE_CAMPAIGN_TOUCH, "DRAFT_CREATED")
    assert result.state_changed and result.resulting_fingerprint != result.plan_fingerprint
    [draft] = campaign_messages(w.db, w.lead)
    assert draft.status is OutboundStatus.DRAFTED and w.transport.calls == []
    assert w.plan().action is A.REVIEW_CAMPAIGN_DRAFT
    w.app.stop()


def test_a_stale_plan_is_refused_and_nothing_runs(db_path: Path) -> None:
    w = world(db_path)
    enrolled(w)
    stale = w.plan()
    suppress(w)  # DNC arrives after planning
    before = table_rows(w.db)
    result = w.app.execution_once(w.lead, stale.fingerprint)
    assert (result.outcome, result.reason, result.state_changed) == (X.STALE_PLAN, "REPLAN_REQUIRED", False)
    assert result.resulting_fingerprint == w.plan().fingerprint and table_rows(w.db) == before
    assert campaign_messages(w.db, w.lead) == []
    w.app.stop()


def test_operator_and_customer_actions_are_reported_not_performed(db_path: Path) -> None:
    w = world(db_path)
    enrolled(w)
    w.execute()
    before = table_rows(w.db)
    review = w.execute(dispatch=True)
    assert (review.outcome, review.reason, review.state_changed) == (X.REQUIRES_OPERATOR, "REVIEW_CAMPAIGN_DRAFT", False)
    assert table_rows(w.db) == before  # never impersonates the operator
    approve_pending(w)
    w.execute(dispatch=True)
    waiting = w.execute()
    assert waiting.outcome is X.REQUIRES_CUSTOMER and waiting.planned_action is A.WAIT_FOR_CUSTOMER
    w.app.stop()


def test_dispatch_needs_an_explicit_request(db_path: Path) -> None:
    w = world(db_path)
    enrolled(w)
    w.execute()
    approve_pending(w)
    refused = w.execute()
    assert (refused.outcome, refused.reason) == (X.BLOCKED, "DISPATCH_NOT_REQUESTED") and w.transport.calls == []
    sent = w.execute(dispatch=True)
    assert (sent.outcome, sent.subsystem_outcome) == (X.EXECUTED, "ACCEPTED") and len(w.transport.calls) == 1
    w.app.stop()


def test_kill_switch_refusal_is_explicit(db_path: Path) -> None:
    w = world(db_path, kill_switch=KillSwitchState(enabled=True, reason="incident", changed_at=NOW, changed_by="ops"))
    enrolled(w)
    before = table_rows(w.db)
    result = w.execute()
    assert (result.outcome, result.reason) == (X.BLOCKED, B.KILL_SWITCH_ACTIVE.value) and table_rows(w.db) == before
    w.app.stop()


def test_missing_capability_returns_a_structured_result(db: Database) -> None:
    from tests.dispatch.builders import approved_reply
    outbound_id = approved_reply(db)
    with db.transaction() as uow:
        lead_id = uow.outbound.get(outbound_id).lead_id  # type: ignore[union-attr]
    service = orchestrator(db, capabilities=OFFLINE)
    result = service.execute(lead_id, service.plan(lead_id).fingerprint, correlation_id="c", allow_dispatch=True)
    assert (result.outcome, result.reason) == (X.CAPABILITY_UNAVAILABLE, B.PROVIDER_CAPABILITY_MISSING.value)
    assert "capability:DISPATCH" in result.reason_codes


def test_a_replayed_old_plan_after_execution_is_stale_and_has_no_side_effect(db_path: Path) -> None:
    w = world(db_path)
    enrolled(w)
    plan = w.plan()
    assert w.app.execution_once(w.lead, plan.fingerprint).outcome is X.EXECUTED
    again = w.app.execution_once(w.lead, plan.fingerprint)
    assert again.outcome is X.STALE_PLAN and len(campaign_messages(w.db, w.lead)) == 1
    w.app.stop()


def test_execution_ids_replay_the_same_plan_and_reject_collisions(db_path: Path) -> None:
    w = world(db_path)
    enrolled(w)
    plan = w.plan()
    first = w.app.execution_once(w.lead, plan.fingerprint, execution_id="exec-1")
    replay = w.app.execution_once(w.lead, plan.fingerprint, execution_id="exec-1")
    assert first.outcome is X.EXECUTED
    assert (replay.outcome, replay.planned_action, replay.reason) == (X.REPLAYED, A.PREPARE_CAMPAIGN_TOUCH, "EXECUTION_ALREADY_APPLIED")
    assert len(campaign_messages(w.db, w.lead)) == 1
    collision = w.app.execution_once(w.lead, w.plan().fingerprint, execution_id="exec-1")  # same id, other plan
    assert (collision.outcome, collision.reason) == (X.ERROR, "EXECUTION_ID_COLLISION")
    blocked = w.app.execution_once(w.lead, w.plan().fingerprint, execution_id="exec-2")  # not executed: not recorded
    assert blocked.outcome is X.REQUIRES_OPERATOR
    assert w.app.execution_once(w.lead, w.plan().fingerprint, execution_id="exec-2").outcome is X.REQUIRES_OPERATOR
    w.app.stop()


def test_unknown_lead_is_an_error_for_the_caller(db_path: Path) -> None:
    w = world(db_path)
    with pytest.raises(OrchestrationNotFoundError):
        w.app.execution_once("ld_missing", "sxp-0")
    w.app.stop()


def test_a_lost_race_inside_a_subsystem_is_a_stale_plan_and_errors_are_contained(
        db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = world(db_path)
    enrolled(w)

    def conflict(*args: object, **kwargs: object) -> object:
        raise ConcurrencyError("version changed")

    monkeypatch.setattr(w.app.services.campaign_executor, "execute", conflict)
    raced = w.execute()
    assert (raced.outcome, raced.reason) == (X.STALE_PLAN, "CONCURRENT_UPDATE")
    assert raced.state_changed and raced.resulting_fingerprint == w.plan().fingerprint  # the claim had committed

    def bug(*args: object, **kwargs: object) -> object:
        raise RuntimeError("executor bug")

    monkeypatch.setattr(w.app.services.campaign_executor, "execute", bug)
    w.advance(timedelta(hours=1))  # the previous claim's lease expired
    broken = w.execute()
    assert (broken.outcome, broken.reason) == (X.ERROR, "RuntimeError")
    held = w.plan()  # the claim survives the failed execution until its lease expires
    assert held.action is A.PREPARE_CAMPAIGN_TOUCH and held.blockers == (B.WORK_IN_PROGRESS,)
    monkeypatch.undo()
    w.advance(timedelta(hours=1))
    recovered = w.execute()
    assert (recovered.outcome, recovered.subsystem_outcome) == (X.EXECUTED, "DRAFT_CREATED")
    with w.db.transaction() as uow:
        [job] = uow.campaign_jobs.list_for_member(w.member_id or "")
        member = uow.campaign_members.get(w.member_id or "")
    assert job.status is CampaignJobStatus.COMPLETED and member is not None and member.status is CampaignMemberStatus.DRAFTED
    w.app.stop()


def test_follow_up_is_scheduled_then_drafted_when_due_one_step_each(db_path: Path) -> None:
    from tests.inbound.builders import envelope
    w = world(db_path, facts={})
    result = w.app.handle_inbound(envelope("p-1"), correlation_id="corr-in")
    w.lead_id = result.lead_id
    approve_pending(w)
    w.execute(dispatch=True)
    plan = w.plan()
    assert (plan.action, plan.executable) == (A.PROCESS_FOLLOW_UP, True)
    scheduled = w.execute()
    assert scheduled.subsystem_outcome == "SCHEDULED"
    waiting = w.plan()
    assert waiting.action is A.WAIT_FOR_CUSTOMER and waiting.waiting_until is not None
    w.clock.set(waiting.waiting_until + timedelta(minutes=1))
    due = w.plan()
    assert (due.action, due.executable, due.refs.follow_up_id is not None) == (A.PROCESS_FOLLOW_UP, True, True)
    drafted = w.execute(dispatch=True)
    assert drafted.subsystem_outcome == "DRAFT_CREATED" and w.transport.calls[1:] == []
    assert w.plan().action is A.REVIEW_REPLY_DRAFT  # the follow-up draft waits for Stage 7 review
    w.app.stop()


def test_the_executor_has_no_loop_in_a_whole_pass(db_path: Path) -> None:
    w = world(db_path)
    enrolled(w)
    first = w.app.execution_pass(dispatch_approved=True)
    assert (first.considered, first.attempted) == (1, 1) and first.results[0].subsystem_outcome == "DRAFT_CREATED"
    second = w.app.execution_pass(dispatch_approved=True)
    assert (second.considered, second.attempted) == (0, 0) and w.transport.calls == []  # waits for the operator
    w.app.stop()


def test_a_failure_after_the_action_never_hides_the_executed_outcome(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.campaign.builders import activate, add_campaign, add_prospect, enroller
    add_campaign(db)
    activate(db)
    contact = add_prospect(db)
    member_id = enroller(db).enroll("camp-1", contact.contact_id, correlation_id="enroll").member_id
    with db.transaction() as uow:
        lead_id = uow.campaign_members.get(member_id or "").lead_id  # type: ignore[union-attr]
    service = orchestrator(db)
    plan = service.plan(lead_id or "")
    real_plan = service.plan
    calls = []

    def flaky(lead: str):  # noqa: ANN202 - the re-plan after the action fails
        calls.append(lead)
        if len(calls) > 1:
            raise RuntimeError("database went away")
        return real_plan(lead)

    monkeypatch.setattr(service, "plan", flaky)
    result = service.execute(lead_id or "", plan.fingerprint, correlation_id="c", execution_id="exec-flaky")
    assert (result.outcome, result.subsystem_outcome, result.state_changed) == (X.EXECUTED, "DRAFT_CREATED", True)
    assert result.resulting_fingerprint is None and len(campaign_messages(db, lead_id or "")) == 1
