"""The per-lead entry points Stage 14 added to Stage 9/10 (same rules as their batch
forms), and adversarial regressions for the coordinator."""

from datetime import timedelta
from pathlib import Path

from app.core.enums import CampaignJobStatus, FollowUpJobStatus, LeadStage, LeadStatus
from app.orchestration import AUTOMATIC_ACTIONS, OPERATOR_COMMANDS
from app.orchestration import ExecutionAction as A
from app.orchestration import ExecutionOutcome as X
from app.orchestration import ExecutionOwner as O
from app.orchestration.models import PRIORITY
from app.persistence import Database, FrozenClock
from tests.campaign.builders import (
    CAMPAIGN_ID,
    add_campaign,
    add_prospect,
    activate,
    enroller,
    scheduler,
)
from tests.inbound.builders import NOW
from tests.orchestration.builders import approve_pending, enrolled, orchestrator, reject_pending, world
from tests.pipeline.builders import lead, mark_lost, qualifying_lead, reopen


def two_members(db: Database) -> tuple[str, str]:
    add_campaign(db)
    activate(db)
    ids = []
    for email in ("ann@alpha.example", "bob@beta.example"):
        contact = add_prospect(db, email, company_name=None)
        result = enroller(db).enroll(CAMPAIGN_ID, contact.contact_id, correlation_id="enroll")
        assert result.member_id is not None
        ids.append(result.member_id)
    return ids[0], ids[1]


def open_jobs(db: Database, member_id: str) -> list[str]:
    with db.transaction() as uow:
        return [j.job_id for j in uow.campaign_jobs.list_for_member(member_id) if j.status is CampaignJobStatus.SCHEDULED]


def test_schedule_member_opens_only_that_members_touch(db: Database) -> None:
    ann, bob = two_members(db)
    summary = scheduler(db).schedule_member(ann, correlation_id="c")
    assert len(summary.scheduled) == 1 and open_jobs(db, ann) == list(summary.scheduled) and open_jobs(db, bob) == []
    again = scheduler(db).schedule_member(ann, correlation_id="c")
    assert again.scheduled == ()  # idempotent: the touch is already open


def test_schedule_member_refuses_a_stale_snapshot(db: Database) -> None:
    ann, _ = two_members(db)
    with db.transaction() as uow:
        version = uow.campaign_members.get(ann).version  # type: ignore[union-attr]
    stale = scheduler(db).schedule_member(ann, correlation_id="c", expected_version=version - 1 if version > 1 else version + 1)
    assert (stale.blocked_reason, stale.scheduled) == ("MEMBER_CHANGED", ()) and open_jobs(db, ann) == []


def test_pending_touch_predicts_what_schedule_member_opens(db: Database) -> None:
    from app.campaign.scheduler import pending_touch
    ann, _ = two_members(db)
    with db.transaction() as uow:
        member = uow.campaign_members.get(ann)
        assert member is not None
        predicted = pending_touch(uow, member, NOW)
    assert predicted is not None and predicted.touch_no == 1 and predicted.open_job is None
    [job_id] = scheduler(db).schedule_member(ann, correlation_id="c").scheduled
    with db.transaction() as uow:
        opened = pending_touch(uow, uow.campaign_members.get(ann), NOW)  # type: ignore[arg-type]
        job = uow.campaign_jobs.get(job_id)
    assert opened is not None and opened.open_job == job and job is not None and job.due_at == predicted.due_at


def test_claiming_one_job_follows_the_claim_due_rules(db: Database) -> None:
    ann, bob = two_members(db)
    [job_id] = scheduler(db).schedule_member(ann, correlation_id="c").scheduled
    claim = scheduler(db).claim(job_id, "w1", correlation_id="c")
    assert claim is not None and not claim.recovered
    assert scheduler(db).claim(job_id, "w2", correlation_id="c") is None  # held by w1
    later = FrozenClock(NOW + timedelta(hours=1))
    recovered = scheduler(db, later).claim(job_id, "w2", correlation_id="c")
    assert recovered is not None and recovered.recovered and recovered.claim_token != claim.claim_token
    assert scheduler(db).claim("cj_missing", "w1", correlation_id="c") is None
    assert open_jobs(db, bob) == []


def test_follow_up_claim_respects_due_time_and_leases(db_path: Path) -> None:
    from tests.inbound.builders import envelope
    w = world(db_path, facts={})
    result = w.app.handle_inbound(envelope("p-1"), correlation_id="corr-in")
    w.lead_id = result.lead_id
    approve_pending(w)
    w.execute(dispatch=True)
    w.execute()  # schedules the follow-up
    follow_up_id = w.plan().refs.follow_up_id
    assert follow_up_id is not None
    follow_ups = w.app.services.follow_up_scheduler
    assert follow_ups.claim(follow_up_id, "w1", correlation_id="c") is None  # not due yet
    w.clock.set(w.plan().waiting_until + timedelta(minutes=1))  # type: ignore[operator]
    claim = follow_ups.claim(follow_up_id, "w1", correlation_id="c")
    assert claim is not None and follow_ups.claim(follow_up_id, "w2", correlation_id="c") is None
    with w.db.transaction() as uow:
        assert uow.follow_up_jobs.get(follow_up_id).status is FollowUpJobStatus.CLAIMED  # type: ignore[union-attr]
    blocked = w.plan()
    assert blocked.action is A.PROCESS_FOLLOW_UP and [b.value for b in blocked.blockers] == ["WORK_IN_PROGRESS"]
    w.app.stop()


# ---- Adversarial regressions -------------------------------------------------------------------


def test_reopen_resurrects_no_proposal_follow_up_or_campaign(db: Database) -> None:
    lead_id = qualifying_lead(db)
    reject_pending(db)
    mark_lost(db, lead_id)
    reopen(db, lead_id, LeadStage.ENGAGED)
    reopened = lead(db, lead_id)
    assert (reopened.stage, reopened.status) == (LeadStage.ENGAGED, LeadStatus.OPERATOR_OWNED)
    plan = orchestrator(db).plan(lead_id)
    # Stage 12 keeps the qualification facts on reopen: a human reviews them again. Nothing
    # commercial, no follow-up and no campaign comes back on its own.
    assert (plan.owner, plan.action, plan.subsystem.value) == (O.OPERATOR, A.REVIEW_QUALIFICATION, "PIPELINE")
    assert not plan.executable and plan.refs.revision_id is None and plan.refs.follow_up_id is None
    assert plan.refs.member_id is None and plan.refs.opportunity_id is None
    reject_pending(db)
    with db.transaction() as uow:
        assert uow.opportunities.get_active_for_lead(lead_id) is None
        assert all(j.status is not FollowUpJobStatus.SCHEDULED
                   for c in uow.conversations.list_by_lead(lead_id) for j in uow.follow_up_jobs.list_for_conversation(c.conversation_id))


def test_execution_identity_records_carry_no_message_text(db_path: Path) -> None:
    w = world(db_path)
    enrolled(w)
    assert w.app.execution_once(w.lead, w.plan().fingerprint, execution_id="exec-v").outcome is X.EXECUTED
    with w.db.transaction() as uow:
        record = uow.idempotency.get("orchestration:exec-v")
        bodies = [m.body_final for m in uow.outbound.list_by_lead(w.lead)] + [m.subject for m in uow.outbound.list_by_lead(w.lead)]
    assert record is not None and record.operation.startswith(f"orchestration.execute:{w.lead}:PREPARE_CAMPAIGN_TOUCH:sxp-")
    assert all(text not in record.operation for text in bodies)
    w.app.stop()


def test_action_tables_are_complete() -> None:
    assert set(PRIORITY) == set(A)
    waits = {A.NO_ACTION, A.NO_AUTOMATION, A.WAIT_FOR_CUSTOMER}
    assert set(OPERATOR_COMMANDS) == set(A) - AUTOMATIC_ACTIONS - waits
    assert all(OPERATOR_COMMANDS[a] for a in OPERATOR_COMMANDS)


def test_a_send_stage8_would_refuse_is_visible_and_not_retried_by_passes(db_path: Path) -> None:
    from app.operator import PauseCampaign
    from tests.operator.builders import AS_ALICE
    w = world(db_path)
    enrolled(w)
    w.execute()
    approve_pending(w)
    assert w.plan().executable
    with w.db.transaction() as uow:
        campaign = uow.campaigns.get(CAMPAIGN_ID)
    assert campaign is not None
    w.ops.pause_campaign(AS_ALICE, PauseCampaign(command_id="cmd-pause", correlation_id="c", campaign_id=CAMPAIGN_ID,
                                                 expected_campaign_version=campaign.version))
    plan = w.plan()
    assert plan.action is A.SEND_APPROVED_MESSAGE and not plan.executable
    assert [b.value for b in plan.blockers] == ["SEND_BLOCKED_BY_POLICY"]
    assert "send:CAMPAIGN_NOT_ACTIVE" in plan.sources  # Stage 10's own dispatch guard, via Stage 8's gates
    assert w.app.execution_pass(dispatch_approved=True).considered == 0 and w.transport.calls == []
    w.app.stop()
