"""Races, stale claims and crash/restart recovery of campaign execution."""

import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.campaign import ExecutionOutcome
from app.campaign import executor as executor_module
from app.campaign import state as campaign_state
from app.core.enums import (
    CampaignJobStatus,
    CampaignMemberStatus,
    DNCReason,
    DNCScope,
    LeadIntent,
    OutboundStatus,
    RefKind,
)
from app.campaign.composer import Composition
from app.core.models import Campaign, CampaignJob, CampaignMember, DoNotContactEntry, EntityRef, OutboundMessage, ProspectContact
from app.dispatch import DispatchCode, DispatchOutcome, FakeBehavior, FakeEmailTransport, FakeStep, TransportRequest
from app.llm import LLMTask
from app.operator import CancelCampaign, PauseCampaign
from app.persistence import Database, FrozenClock, UnitOfWork
from tests.campaign.builders import (
    CAMPAIGN_ID,
    INTERVAL,
    PROSPECT,
    approve,
    campaign_messages,
    claim_all,
    draft_touch,
    executor,
    member,
    outbound,
    ready_campaign,
    scheduler,
    send_touch,
)
from tests.dispatch.builders import dispatcher, send
from tests.inbound.builders import NOW, ScriptedTransport, classification, envelope, process
from tests.operator.builders import AS_ALICE, operator

M = CampaignMemberStatus
LATER = NOW + INTERVAL


class Crash(BaseException):
    """The process dies (not an Exception, so nothing catches it)."""


def run_concurrently(db_path: Path, *jobs: Callable[[Database], object]) -> list[object]:
    barrier = threading.Barrier(len(jobs))
    results: list[object] = [None] * len(jobs)

    def worker(index: int, fn: Callable[[Database], object]) -> None:
        with Database(db_path, busy_timeout_ms=10_000) as db:
            barrier.wait()
            try:
                results[index] = fn(db)
            except Exception as exc:  # noqa: BLE001 - asserted below
                results[index] = exc

    threads = [threading.Thread(target=worker, args=(i, fn)) for i, fn in enumerate(jobs)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    return results


def customer_replies(db: Database, pid: str = "p-reply") -> None:
    process(db, ScriptedTransport().script(LLMTask.INTENT_CLASSIFICATION, classification(LeadIntent.NEGOTIATION)),
            envelope(pid, sender=PROSPECT, body="Tell me more."))


def sent_first_touch(db: Database) -> tuple[str, str]:
    member_id = ready_campaign(db)
    first = draft_touch(db)
    assert send_touch(db, first.outbound_id or "").outcome is DispatchOutcome.ACCEPTED
    return member_id, first.outbound_id or ""


def pause(db: Database) -> object:
    service = operator(db)
    with db.transaction() as uow:
        campaign = uow.campaigns.get(CAMPAIGN_ID)
    assert campaign is not None
    return service.pause_campaign(AS_ALICE, PauseCampaign(command_id=f"cmd-pause-{campaign.version}", correlation_id="c",
                                                          campaign_id=CAMPAIGN_ID, expected_campaign_version=campaign.version))


def cancel(db: Database) -> object:
    service = operator(db)
    with db.transaction() as uow:
        campaign = uow.campaigns.get(CAMPAIGN_ID)
    assert campaign is not None
    return service.cancel_campaign(AS_ALICE, CancelCampaign(command_id="cmd-cancel", correlation_id="c", campaign_id=CAMPAIGN_ID,
                                                            expected_campaign_version=campaign.version))


def live_drafts(db: Database, member_id: str) -> list[str]:
    return [m.outbound_id for m in campaign_messages(db, member(db, member_id).lead_id or "")
            if m.status in (OutboundStatus.DRAFTED, OutboundStatus.OPERATOR_APPROVED)]


# ---- Reply races ----------------------------------------------------------------------------


def test_reply_before_a_due_follow_up_is_executed_stops_it(db: Database) -> None:
    member_id, _ = sent_first_touch(db)
    later = FrozenClock(LATER)
    scheduler(db, later).schedule(CAMPAIGN_ID, correlation_id="c")
    [claim] = claim_all(db, later)
    customer_replies(db)
    result = executor(db, later).execute(claim, correlation_id="c")
    assert (result.outcome, result.job_status) == (ExecutionOutcome.STALE_CLAIM, CampaignJobStatus.SUPERSEDED)
    assert member(db, member_id).status is M.REPLIED and live_drafts(db, member_id) == []


@pytest.mark.parametrize("round_no", range(3))
def test_reply_racing_draft_execution_never_leaves_a_live_draft(db_path: Path, round_no: int) -> None:
    with Database(db_path) as db:
        member_id, _ = sent_first_touch(db)
        scheduler(db, FrozenClock(LATER)).schedule(CAMPAIGN_ID, correlation_id="c")
        [claim] = claim_all(db, FrozenClock(LATER))
    results = run_concurrently(db_path, lambda db: executor(db, FrozenClock(LATER)).execute(claim, correlation_id="c"),
                               lambda db: customer_replies(db))
    assert not any(isinstance(r, Exception) for r in results), results
    with Database(db_path) as db:
        assert member(db, member_id).status is M.REPLIED and live_drafts(db, member_id) == []


@pytest.mark.parametrize("action", ["pause", "cancel"])
def test_pause_or_cancel_racing_the_worker_never_leaves_live_work(db_path: Path, action: str) -> None:
    with Database(db_path) as db:
        member_id = ready_campaign(db)
        scheduler(db).schedule(CAMPAIGN_ID, correlation_id="c")
        [claim] = claim_all(db, FrozenClock(NOW))
    control = pause if action == "pause" else cancel
    results = run_concurrently(db_path, lambda db: executor(db).execute(claim, correlation_id="c"), control)
    assert not any(isinstance(r, Exception) for r in results), results
    with Database(db_path) as db:
        current = member(db, member_id)
        if action == "cancel":
            assert current.status is M.CANCELLED and live_drafts(db, member_id) == []
        else:  # a draft may exist (made before the pause) but can never be dispatched while paused
            for draft in live_drafts(db, member_id):
                approve_blocked = operator(db).get_draft(AS_ALICE, draft)
                assert not approve_blocked.actionable
        assert claim_all(db, FrozenClock(NOW + timedelta(hours=2))) == ()


def test_dnc_added_after_claim_suppresses_instead_of_drafting(db: Database) -> None:
    member_id = ready_campaign(db)
    scheduler(db).schedule(CAMPAIGN_ID, correlation_id="c")
    [claim] = claim_all(db, FrozenClock(NOW))
    with db.transaction() as uow:
        uow.dnc.add(DoNotContactEntry(entry_id="dnc-1", scope=DNCScope.EMAIL, value=PROSPECT, reason=DNCReason.OPERATOR,
                                      source_ref=EntityRef(kind=RefKind.OPERATOR_COMMAND, id="c"), created_by="op", created_at=NOW))
    result = executor(db).execute(claim, correlation_id="c")
    assert (result.outcome, result.member_status) == (ExecutionOutcome.BLOCKED, M.SUPPRESSED)
    assert live_drafts(db, member_id) == []


# ---- Duplicate prevention -------------------------------------------------------------------


@pytest.mark.parametrize("round_no", range(3))
def test_two_workers_create_one_draft(db_path: Path, round_no: int) -> None:
    with Database(db_path) as db:
        member_id = ready_campaign(db)
        scheduler(db).schedule(CAMPAIGN_ID, correlation_id="c")

    def tick(worker: str) -> Callable[[Database], object]:
        return lambda db: [executor(db).execute(c, correlation_id=worker) for c in claim_all(db, FrozenClock(NOW), worker)]

    results = run_concurrently(db_path, tick("w1"), tick("w2"))
    outcomes = [r.outcome for batch in results if isinstance(batch, list) for r in batch]
    assert outcomes == [ExecutionOutcome.DRAFT_CREATED], results
    with Database(db_path) as db:
        assert len(campaign_messages(db, member(db, member_id).lead_id or "")) == 1


@pytest.mark.parametrize("round_no", range(3))
def test_two_schedulers_open_each_touch_once(db_path: Path, round_no: int) -> None:
    with Database(db_path) as db:
        member_id = ready_campaign(db)
    results = run_concurrently(db_path, lambda db: scheduler(db).schedule(CAMPAIGN_ID, correlation_id="a"),
                               lambda db: scheduler(db).schedule(CAMPAIGN_ID, correlation_id="b"))
    assert not any(isinstance(r, Exception) for r in results), results
    with Database(db_path) as db, db.transaction() as uow:
        assert len(uow.campaign_jobs.list_for_member(member_id)) == 1


def test_stale_claim_token_cannot_act_after_recovery(db: Database) -> None:
    member_id = ready_campaign(db)
    scheduler(db).schedule(CAMPAIGN_ID, correlation_id="c")
    [old] = claim_all(db, FrozenClock(NOW), "worker-a")  # worker-a stalls
    after = FrozenClock(old.lease_expires_at)
    [recovered] = claim_all(db, after, "worker-b")
    assert recovered.recovered and recovered.claim_token != old.claim_token
    assert executor(db, after).execute(old, correlation_id="late").outcome is ExecutionOutcome.STALE_CLAIM
    assert executor(db, after).execute(recovered, correlation_id="c").outcome is ExecutionOutcome.DRAFT_CREATED
    assert len(campaign_messages(db, member(db, member_id).lead_id or "")) == 1


# ---- Crash and restart ---------------------------------------------------------------------


def test_jobs_survive_a_restart(db_path: Path) -> None:
    with Database(db_path) as db:
        ready_campaign(db)
        scheduler(db).schedule(CAMPAIGN_ID, correlation_id="c")
    with Database(db_path) as restarted:
        [claim] = claim_all(restarted, FrozenClock(NOW))
        assert executor(restarted).execute(claim, correlation_id="c").outcome is ExecutionOutcome.DRAFT_CREATED


def test_crash_after_draft_creation_rolls_back_and_recovers_once(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    member_id = ready_campaign(db)
    scheduler(db).schedule(CAMPAIGN_ID, correlation_id="c")
    [claim] = claim_all(db, FrozenClock(NOW))
    real = executor_module.CampaignExecutor._draft

    def crash_after(self: executor_module.CampaignExecutor, uow: UnitOfWork, job: CampaignJob, member_: CampaignMember,
                    campaign: Campaign, contact: ProspectContact, composition: Composition, now: datetime) -> OutboundMessage:
        real(self, uow, job, member_, campaign, contact, composition, now)
        raise Crash()

    monkeypatch.setattr(executor_module.CampaignExecutor, "_draft", crash_after)
    with pytest.raises(Crash):
        executor(db).execute(claim, correlation_id="c")
    monkeypatch.undo()
    assert campaign_messages(db, member(db, member_id).lead_id or "") == []
    after = FrozenClock(claim.lease_expires_at)
    [again] = claim_all(db, after)
    assert executor(db, after).execute(again, correlation_id="c").outcome is ExecutionOutcome.DRAFT_CREATED
    assert len(campaign_messages(db, member(db, member_id).lead_id or "")) == 1


def test_crash_around_the_reply_handoff_never_leaves_both_automations_active(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    member_id, first = sent_first_touch(db)
    real = campaign_state.record_contact_reply

    def crash(*args: object, **kwargs: object) -> object:
        raise RuntimeError("storage failure during handoff")

    monkeypatch.setattr(campaign_state, "record_contact_reply", crash)
    contact_id = member(db, member_id).contact_id
    thread_id = member(db, member_id).thread_id or ""
    with pytest.raises(RuntimeError):
        customer_replies(db)
    with db.transaction() as uow:  # nothing of the observation was committed
        assert uow.conversations.list_by_contact(contact_id) == []
        thread = uow.threads.get(thread_id)
    assert thread is not None and len(thread.message_ids) == 1  # only our first touch
    assert member(db, member_id).status is M.WAITING
    monkeypatch.setattr(campaign_state, "record_contact_reply", real)
    customer_replies(db)  # the provider redelivers the same message
    with db.transaction() as uow:
        conversations = uow.conversations.list_by_contact(contact_id)
    assert member(db, member_id).status is M.REPLIED and len(conversations) == 1


def test_crash_after_the_dispatch_claim_blocks_the_next_touch_and_any_resend(db: Database) -> None:
    member_id = ready_campaign(db)
    first = draft_touch(db)
    approve(db, first.outbound_id or "")

    def die(_: TransportRequest) -> None:
        raise Crash()

    transport = FakeEmailTransport().script(FakeStep(FakeBehavior.ACCEPT, before=die))
    with pytest.raises(Crash):
        send(dispatcher(db, transport), first.outbound_id or "")
    assert member(db, member_id).status is M.DISPATCHING
    again = send(dispatcher(db, transport), first.outbound_id or "", "corr-restart")
    assert again.reason_codes == (DispatchCode.ATTEMPT_UNRESOLVED,) and len(transport.calls) == 1
    assert scheduler(db, FrozenClock(LATER)).schedule(CAMPAIGN_ID, correlation_id="c").scheduled == ()
    assert outbound(db, first.outbound_id or "").status is OutboundStatus.SENDING
