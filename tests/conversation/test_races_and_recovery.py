"""Reply races, duplicate prevention under concurrency, crash and restart recovery, and
the interaction with Stage 8 unresolved or conflicting dispatches."""

import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.core.enums import ConversationStatus, FollowUpJobStatus, OutboundStatus
from app.conversation import ExecutionOutcome, FollowUpBlock, ScheduleOutcome, ScheduleResult
from app.conversation import executor as executor_module
from app.dispatch import DispatchCode, DispatchOutcome, FakeBehavior, FakeEmailTransport, FakeStep
from app.operator import PauseConversation
from app.persistence import Database, DispatchAttemptState, FrozenClock, UnitOfWork
from tests.conversation.builders import (
    FIRST_DUE,
    approve,
    claim_one,
    conversation,
    customer_writes,
    executor,
    follow_up_drafts,
    follow_up_to_draft,
    job,
    jobs,
    replied_conversation,
    scheduler,
)
from tests.dispatch.builders import dispatcher, send, state
from tests.operator.builders import AS_ALICE, operator
from app.core.models import Conversation, EmailThread, FollowUpJob, OutboundMessage
from tests.inbound.builders import MAILBOX, NOW, SENDER

LATER = FIRST_DUE + timedelta(minutes=1)


def schedule(db: Database, conversation_id: str) -> str:
    result = scheduler(db).schedule(conversation_id, correlation_id="corr-s")
    assert result.outcome is ScheduleOutcome.SCHEDULED and result.follow_up_id is not None
    return result.follow_up_id


def run_concurrently(db_path: Path, *jobs_: Callable[[Database], object]) -> list[object]:
    barrier = threading.Barrier(len(jobs_))
    results: list[object] = [None] * len(jobs_)

    def worker(index: int, fn: Callable[[Database], object]) -> None:
        with Database(db_path, busy_timeout_ms=10_000) as db:
            barrier.wait()
            try:
                results[index] = fn(db)
            except Exception as exc:  # noqa: BLE001 - asserted by the tests
                results[index] = exc

    threads = [threading.Thread(target=worker, args=(i, fn)) for i, fn in enumerate(jobs_)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    return results


# ---- Reply races ---------------------------------------------------------------------------------


def test_inbound_reply_supersedes_a_scheduled_follow_up(db: Database) -> None:
    replied = replied_conversation(db)
    follow_up_id = schedule(db, replied.conversation_id)
    customer_writes(db, "p-2", received_at=NOW + timedelta(days=1))
    assert job(db, follow_up_id).status is FollowUpJobStatus.SUPERSEDED
    assert conversation(db, replied.conversation_id).status is not ConversationStatus.FOLLOW_UP_DUE
    assert scheduler(db, FrozenClock(LATER)).claim_due("w", correlation_id="c") == ()


def test_inbound_between_claim_and_execution_blocks_the_stale_follow_up(db: Database) -> None:
    replied = replied_conversation(db)
    schedule(db, replied.conversation_id)
    clock = FrozenClock(LATER)
    claim = claim_one(db, clock)
    customer_writes(db, "p-2", received_at=FIRST_DUE)  # arrives after the claim
    result = executor(db, clock).execute(claim, correlation_id="corr-exec")
    assert result.outcome is ExecutionOutcome.STALE_CLAIM and result.job_status is FollowUpJobStatus.SUPERSEDED
    assert follow_up_drafts(db, replied.lead_id) == []


def test_stale_scheduler_snapshot_cannot_schedule_after_newer_activity(db: Database) -> None:
    replied = replied_conversation(db)
    seen = conversation(db, replied.conversation_id).version
    customer_writes(db, "p-2", received_at=NOW + timedelta(hours=1))
    stale = scheduler(db).schedule(replied.conversation_id, correlation_id="c", expected_version=seen)
    assert stale.outcome is ScheduleOutcome.STALE_SNAPSHOT and jobs(db, replied.conversation_id) == []
    fresh = scheduler(db).schedule(replied.conversation_id, correlation_id="c2")
    assert fresh.outcome is ScheduleOutcome.BLOCKED and FollowUpBlock.CONVERSATION_NOT_WAITING in fresh.reason_codes


def test_inbound_after_the_draft_cancels_it_and_it_can_never_be_dispatched(db: Database) -> None:
    replied = replied_conversation(db)
    _, draft = follow_up_to_draft(db, replied.conversation_id)
    approve(db, draft, FrozenClock(LATER))
    customer_writes(db, "p-2", received_at=LATER)
    [cancelled] = follow_up_drafts(db, replied.lead_id)
    assert cancelled.status is OutboundStatus.CANCELLED
    transport = FakeEmailTransport()
    result = send(dispatcher(db, transport, clock=FrozenClock(LATER)), draft)
    assert result.outcome is DispatchOutcome.BLOCKED and transport.calls == []


@pytest.mark.parametrize("round_no", range(3))
def test_racing_execution_and_inbound_never_leave_a_live_stale_draft(db_path: Path, round_no: int) -> None:
    with Database(db_path) as db:
        replied = replied_conversation(db)
        schedule(db, replied.conversation_id)
        claim = claim_one(db, FrozenClock(LATER))
    results = run_concurrently(
        db_path,
        lambda db: executor(db, FrozenClock(LATER)).execute(claim, correlation_id="corr-exec"),
        lambda db: customer_writes(db, "p-2", received_at=LATER),
    )
    assert not any(isinstance(r, Exception) for r in results), results
    with Database(db_path) as db:
        drafts = follow_up_drafts(db, replied.lead_id)
        assert all(d.status is OutboundStatus.CANCELLED for d in drafts) and len(drafts) <= 1
        assert jobs(db, replied.conversation_id)[0].status in (FollowUpJobStatus.SUPERSEDED, FollowUpJobStatus.COMPLETED)


# ---- Duplicate prevention -----------------------------------------------------------------------


@pytest.mark.parametrize("round_no", range(3))
def test_two_workers_produce_one_logical_follow_up(db_path: Path, round_no: int) -> None:
    with Database(db_path) as db:
        replied = replied_conversation(db)
        schedule(db, replied.conversation_id)

    def tick(worker: str) -> Callable[[Database], object]:
        return lambda db: [executor(db, FrozenClock(LATER)).execute(c, correlation_id=worker)
                           for c in scheduler(db, FrozenClock(LATER)).claim_due(worker, correlation_id=worker)]

    results = run_concurrently(db_path, tick("w1"), tick("w2"))
    assert not any(isinstance(r, Exception) for r in results), results
    outcomes = [r.outcome for batch in results if isinstance(batch, list) for r in batch]
    assert outcomes == [ExecutionOutcome.DRAFT_CREATED]
    with Database(db_path) as db:
        assert len(follow_up_drafts(db, replied.lead_id)) == 1


@pytest.mark.parametrize("round_no", range(3))
def test_concurrent_schedulers_create_one_job(db_path: Path, round_no: int) -> None:
    with Database(db_path) as db:
        replied = replied_conversation(db)
    results = run_concurrently(
        db_path,
        lambda db: scheduler(db).schedule(replied.conversation_id, correlation_id="a"),
        lambda db: scheduler(db).schedule(replied.conversation_id, correlation_id="b"),
    )
    assert sorted(r.outcome.value for r in results if isinstance(r, ScheduleResult)) == ["ALREADY_SCHEDULED", "SCHEDULED"]
    with Database(db_path) as db:
        assert len(jobs(db, replied.conversation_id)) == 1


def test_executing_the_same_claim_twice_is_idempotent(db: Database) -> None:
    replied = replied_conversation(db)
    schedule(db, replied.conversation_id)
    clock = FrozenClock(LATER)
    claim = claim_one(db, clock)
    first = executor(db, clock).execute(claim, correlation_id="c1")
    again = executor(db, clock).execute(claim, correlation_id="c2")
    assert (first.outcome, again.outcome) == (ExecutionOutcome.DRAFT_CREATED, ExecutionOutcome.REPLAYED)
    assert again.outbound_id == first.outbound_id and len(follow_up_drafts(db, replied.lead_id)) == 1


# ---- Crash and restart recovery -------------------------------------------------------------------


def test_scheduled_jobs_survive_a_restart(db_path: Path) -> None:
    with Database(db_path) as db:
        replied = replied_conversation(db)
        follow_up_id = schedule(db, replied.conversation_id)
    with Database(db_path) as restarted:
        [claim] = scheduler(restarted, FrozenClock(LATER)).claim_due("w", correlation_id="c")
        assert claim.follow_up_id == follow_up_id
        assert executor(restarted, FrozenClock(LATER)).execute(claim, correlation_id="c").outcome is ExecutionOutcome.DRAFT_CREATED


def test_an_expired_claim_is_recovered_and_the_old_worker_cannot_act(db: Database) -> None:
    replied = replied_conversation(db)
    schedule(db, replied.conversation_id)
    old = claim_one(db, FrozenClock(LATER), "worker-a")  # worker-a then crashes
    assert scheduler(db, FrozenClock(LATER + timedelta(minutes=1))).claim_due("worker-b", correlation_id="c") == ()  # lease live
    after_lease = FrozenClock(old.lease_expires_at)
    [recovered] = scheduler(db, after_lease).claim_due("worker-b", correlation_id="c")
    assert recovered.recovered and recovered.claim_token != old.claim_token
    assert executor(db, after_lease).execute(old, correlation_id="late").outcome is ExecutionOutcome.STALE_CLAIM
    assert executor(db, after_lease).execute(recovered, correlation_id="c").outcome is ExecutionOutcome.DRAFT_CREATED
    assert len(follow_up_drafts(db, replied.lead_id)) == 1


def test_crash_during_execution_rolls_back_and_recovers_without_duplicates(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    replied = replied_conversation(db)
    schedule(db, replied.conversation_id)
    claim = claim_one(db, FrozenClock(LATER))

    class Crash(BaseException):
        pass

    real = executor_module.FollowUpExecutor._draft

    def crash_after_draft(self: executor_module.FollowUpExecutor, uow: UnitOfWork, job: FollowUpJob, conversation: Conversation,
                          subject: str, body: str, now: datetime) -> OutboundMessage:
        real(self, uow, job, conversation, subject, body, now)
        raise Crash()

    monkeypatch.setattr(executor_module.FollowUpExecutor, "_draft", crash_after_draft)
    with pytest.raises(Crash):
        executor(db, FrozenClock(LATER)).execute(claim, correlation_id="c")
    monkeypatch.undo()
    assert follow_up_drafts(db, replied.lead_id) == [] and job(db, claim.follow_up_id).status is FollowUpJobStatus.CLAIMED
    after_lease = FrozenClock(claim.lease_expires_at)
    [recovered] = scheduler(db, after_lease).claim_due("w2", correlation_id="c")
    assert executor(db, after_lease).execute(recovered, correlation_id="c").outcome is ExecutionOutcome.DRAFT_CREATED
    assert len(follow_up_drafts(db, replied.lead_id)) == 1


def test_crash_after_dispatch_claim_never_creates_a_second_dispatch_or_follow_up(db: Database) -> None:
    replied = replied_conversation(db)
    _, draft = follow_up_to_draft(db, replied.conversation_id)
    approve(db, draft, FrozenClock(LATER))
    transport = FakeEmailTransport().script(FakeBehavior.TIMEOUT)
    assert send(dispatcher(db, transport, clock=FrozenClock(LATER)), draft).outcome is DispatchOutcome.UNKNOWN
    # Restart: re-running dispatch and the follow-up worker creates nothing new.
    again = send(dispatcher(db, transport, clock=FrozenClock(LATER)), draft, "corr-again")
    assert again.reason_codes == (DispatchCode.ATTEMPT_UNRESOLVED,) and len(transport.calls) == 1
    later = FrozenClock(LATER + timedelta(days=10))
    blocked = scheduler(db, later).schedule(replied.conversation_id, correlation_id="c")
    assert blocked.outcome is ScheduleOutcome.BLOCKED and FollowUpBlock.DISPATCH_UNRESOLVED in blocked.reason_codes
    assert scheduler(db, later).claim_due("w", correlation_id="c") == ()
    assert len(follow_up_drafts(db, replied.lead_id)) == 1


def test_late_acceptance_conflict_on_an_old_dispatch_blocks_newer_follow_ups(db: Database) -> None:
    replied = replied_conversation(db)
    follow_up_id = schedule(db, replied.conversation_id)
    # An older message of the lead acquires late-acceptance conflict evidence (Stage 8).
    reply = state(db, replied.reply_outbound_id)
    with db.transaction() as uow:
        attempt = reply.attempts[0]
        uow.dispatch_attempts.update(attempt.model_copy(update={
            "state": DispatchAttemptState.NOT_ACCEPTED, "reason_code": "X", "provider_message_id": None,
            "late_acceptance_provider_message_id": "fake-late", "late_acceptance_at": NOW, "version": attempt.version + 1,
        }), attempt.version)
    clock = FrozenClock(LATER)
    result = executor(db, clock).execute(claim_one(db, clock), correlation_id="c")
    assert result.outcome is ExecutionOutcome.BLOCKED and FollowUpBlock.ACCEPTANCE_CONFLICT in result.reason_codes
    assert job(db, follow_up_id).status is FollowUpJobStatus.BLOCKED and follow_up_drafts(db, replied.lead_id) == []


def test_conflict_arising_after_approval_still_stops_the_follow_up_dispatch(db: Database) -> None:
    replied = replied_conversation(db)
    _, draft = follow_up_to_draft(db, replied.conversation_id)
    approve(db, draft, FrozenClock(LATER))
    reply = state(db, replied.reply_outbound_id)
    with db.transaction() as uow:
        attempt = reply.attempts[0]
        uow.dispatch_attempts.update(attempt.model_copy(update={
            "state": DispatchAttemptState.NOT_ACCEPTED, "reason_code": "X", "provider_message_id": None,
            "late_acceptance_provider_message_id": "fake-late", "late_acceptance_at": NOW, "version": attempt.version + 1,
        }), attempt.version)
    transport = FakeEmailTransport()
    blocked = send(dispatcher(db, transport, clock=FrozenClock(LATER)), draft)
    assert blocked.outcome is DispatchOutcome.BLOCKED and FollowUpBlock.ACCEPTANCE_CONFLICT.value in blocked.reason_codes
    assert transport.calls == []


# ---- Adversarial review regressions -----------------------------------------------------------


def test_unresolved_dispatch_on_another_lead_of_the_contact_blocks_follow_ups(db: Database) -> None:
    replied = replied_conversation(db)
    schedule(db, replied.conversation_id)
    # The same person has a second lead (another thread) whose message is in flight.
    with db.transaction() as uow:
        first_lead = uow.leads.get(replied.lead_id)
        reply = uow.outbound.get(replied.reply_outbound_id)
        assert first_lead is not None and reply is not None
        uow.leads.add(first_lead.model_copy(update={"lead_id": "lead-2", "version": 1}))
        uow.threads.add(EmailThread(thread_id="th-2", mailbox=MAILBOX, participant_addresses=(MAILBOX, SENDER),
                                    subject_normalized="other", lead_id="lead-2"))
        uow.outbound.add(reply.model_copy(update={
            "outbound_id": "ob-inflight", "idempotency_key": "inflight", "lead_id": "lead-2", "thread_id": "th-2",
            "status": OutboundStatus.SENDING, "sent_at": None, "provider_message_id": None, "version": 1,
        }))
    result = executor(db, FrozenClock(LATER)).execute(claim_one(db, FrozenClock(LATER)), correlation_id="c")
    assert result.outcome is ExecutionOutcome.BLOCKED and FollowUpBlock.DISPATCH_UNRESOLVED in result.reason_codes


def test_inbound_during_an_in_flight_follow_up_dispatch_is_recorded_honestly(db: Database) -> None:
    replied = replied_conversation(db)
    _, draft = follow_up_to_draft(db, replied.conversation_id)
    approve(db, draft, FrozenClock(LATER))

    def customer_replies(_: object) -> None:
        customer_writes(db, "p-2", received_at=LATER)

    transport = FakeEmailTransport().script(FakeStep(FakeBehavior.ACCEPT, before=customer_replies))
    assert send(dispatcher(db, transport, clock=FrozenClock(LATER)), draft).outcome is DispatchOutcome.ACCEPTED
    after = conversation(db, replied.conversation_id)
    # The hand-off was already in flight (not cancellable); the customer wrote after our
    # approval, so the conversation needs our response rather than waiting for theirs.
    assert after.status is not ConversationStatus.WAITING_FOR_REPLY and after.follow_up_count == 1
    assert scheduler(db, FrozenClock(LATER + timedelta(days=5))).schedule(replied.conversation_id, correlation_id="c").outcome is ScheduleOutcome.BLOCKED


@pytest.mark.parametrize("round_no", range(3))
def test_racing_execution_and_operator_pause_never_leave_a_live_follow_up(db_path: Path, round_no: int) -> None:
    with Database(db_path) as db:
        replied = replied_conversation(db)
        schedule(db, replied.conversation_id)
        claim = claim_one(db, FrozenClock(LATER))

    def pause(db: Database) -> object:
        service = operator(db, FrozenClock(LATER))
        current = service.get_conversation(AS_ALICE, replied.conversation_id).version
        return service.pause_conversation(AS_ALICE, PauseConversation(
            command_id="cmd-pause", correlation_id="c", conversation_id=replied.conversation_id, expected_conversation_version=current))

    results = run_concurrently(db_path, lambda db: executor(db, FrozenClock(LATER)).execute(claim, correlation_id="c"), pause)
    assert not any(isinstance(r, Exception) for r in results), results
    with Database(db_path) as db:
        assert conversation(db, replied.conversation_id).status is ConversationStatus.PAUSED
        assert all(d.status is OutboundStatus.CANCELLED for d in follow_up_drafts(db, replied.lead_id))
