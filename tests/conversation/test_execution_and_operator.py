"""Execution-time revalidation (policy, quota, DNC, pause) and operator conversation actions."""

from datetime import timedelta

import pytest

from app.core.enums import ConversationStatus, DNCReason, DNCScope, FollowUpJobStatus, OutboundStatus, RefKind
from app.core.models import DoNotContactEntry, EntityRef
from app.conversation import ExecutionOutcome, ExecutionResult, FollowUpBlock, ScheduleOutcome
from app.dispatch import DispatchOutcome, FakeEmailTransport
from app.operator import (
    BlockCode,
    CancelFollowUp,
    CloseConversation,
    CommandRejectedError,
    MarkDoNotContact,
    OperatorService,
    PauseConversation,
    ResumeConversation,
    StaleCommandError,
)
from app.persistence import Database, FrozenClock
from app.policy import KillSwitchState, PolicyReason
from tests.conversation.builders import (
    FIRST_DUE,
    approve,
    claim_one,
    conversation,
    executor,
    follow_up_drafts,
    follow_up_to_draft,
    job,
    one_slot,
    replied_conversation,
    scheduler,
)
from tests.dispatch.builders import dispatcher, send
from tests.inbound.builders import NOW, SENDER, envelope, happy_transport, process
from tests.operator.builders import AS_ALICE, operator, reject_command

LATER = FIRST_DUE + timedelta(minutes=1)


def scheduled(db: Database) -> tuple[str, str, str]:
    replied = replied_conversation(db)
    result = scheduler(db).schedule(replied.conversation_id, correlation_id="corr-s")
    assert result.follow_up_id is not None
    return replied.conversation_id, result.follow_up_id, replied.lead_id


def execute(db: Database, clock: FrozenClock | None = None, **config: object) -> ExecutionResult:
    clock = clock or FrozenClock(LATER)
    return executor(db, clock, **config).execute(claim_one(db, clock), correlation_id="corr-exec")


# ---- Execution-time revalidation --------------------------------------------------------------


def test_quota_exhausted_at_execution_defers_then_proceeds(db: Database) -> None:
    conversation_id, follow_up_id, lead_id = scheduled(db)
    # Another customer's reply is accepted on the due day and takes that day's only slot.
    busy = process(db, happy_transport(), envelope("p-2", sender="other@elsewhere.example"))
    assert busy.outbound_id is not None
    approve(db, busy.outbound_id, FrozenClock(LATER))
    assert send(dispatcher(db, clock=FrozenClock(LATER), limits=one_slot()), busy.outbound_id).outcome is DispatchOutcome.ACCEPTED

    result = execute(db, FrozenClock(LATER), limits=one_slot())
    assert result.outcome is ExecutionOutcome.DEFERRED and result.reason_codes == (PolicyReason.GLOBAL_DAILY_LIMIT.value,)
    deferred = job(db, follow_up_id)
    assert deferred.status is FollowUpJobStatus.SCHEDULED and deferred.due_at == LATER + timedelta(hours=1)
    assert conversation(db, conversation_id).next_follow_up_at == deferred.due_at and follow_up_drafts(db, lead_id) == []
    next_day = FrozenClock(LATER + timedelta(days=1))
    assert execute(db, next_day, limits=one_slot()).outcome is ExecutionOutcome.DRAFT_CREATED


def test_sending_window_and_kill_switch_defer_execution(db: Database) -> None:
    _, follow_up_id, _ = scheduled(db)
    evening = FrozenClock(FIRST_DUE.replace(hour=17))  # 20:00 in Kyiv
    assert execute(db, evening).reason_codes == (PolicyReason.OUTSIDE_SENDING_WINDOW.value,)
    stop = KillSwitchState(enabled=True, reason="incident", changed_at=NOW, changed_by="ops")
    morning = FrozenClock(FIRST_DUE.replace(hour=17) + timedelta(hours=16))
    assert execute(db, morning, kill_switch=stop).outcome is ExecutionOutcome.DEFERRED
    assert job(db, follow_up_id).status is FollowUpJobStatus.SCHEDULED


def test_dnc_added_after_scheduling_blocks_execution(db: Database) -> None:
    conversation_id, follow_up_id, lead_id = scheduled(db)
    with db.transaction() as uow:
        uow.dnc.add(DoNotContactEntry(entry_id="dnc-1", scope=DNCScope.EMAIL, value=SENDER, reason=DNCReason.OPERATOR,
                                      source_ref=EntityRef(kind=RefKind.OPERATOR_COMMAND, id="c"), created_by="op", created_at=NOW))
    result = execute(db)
    assert result.outcome is ExecutionOutcome.BLOCKED and FollowUpBlock.DO_NOT_CONTACT in result.reason_codes
    assert job(db, follow_up_id).status is FollowUpJobStatus.BLOCKED and follow_up_drafts(db, lead_id) == []
    assert conversation(db, conversation_id).status is ConversationStatus.WAITING_FOR_REPLY


def test_job_is_not_executed_before_it_is_due(db: Database) -> None:
    scheduled(db)
    assert scheduler(db, FrozenClock(FIRST_DUE - timedelta(seconds=1))).claim_due("w", correlation_id="c") == ()


# ---- Operator actions ---------------------------------------------------------------------------


def version(service: OperatorService, conversation_id: str) -> int:
    return service.get_conversation(AS_ALICE, conversation_id).version


def test_pause_after_scheduling_blocks_execution_and_resume_allows_rescheduling(db: Database) -> None:
    conversation_id, follow_up_id, _ = scheduled(db)
    service = operator(db)
    service.pause_conversation(AS_ALICE, PauseConversation(command_id="cmd-p", correlation_id="c", conversation_id=conversation_id,
                                                           expected_conversation_version=version(service, conversation_id)))
    assert job(db, follow_up_id).status is FollowUpJobStatus.CANCELLED
    assert scheduler(db, FrozenClock(LATER)).claim_due("w", correlation_id="c") == ()
    assert FollowUpBlock.CONVERSATION_PAUSED in scheduler(db).schedule(conversation_id, correlation_id="c").reason_codes

    service.resume_conversation(AS_ALICE, ResumeConversation(command_id="cmd-r", correlation_id="c", conversation_id=conversation_id,
                                                             expected_conversation_version=version(service, conversation_id)))
    assert conversation(db, conversation_id).status is ConversationStatus.WAITING_FOR_REPLY
    again = scheduler(db).schedule(conversation_id, correlation_id="c2")
    assert again.outcome is ScheduleOutcome.SCHEDULED and again.follow_up_id == follow_up_id  # same logical follow-up, reopened


def test_pause_after_approval_stops_the_follow_up_dispatch(db: Database) -> None:
    replied = replied_conversation(db)
    _, draft = follow_up_to_draft(db, replied.conversation_id)
    approve(db, draft, FrozenClock(LATER))
    service = operator(db, FrozenClock(LATER))
    service.pause_conversation(AS_ALICE, PauseConversation(command_id="cmd-p", correlation_id="c", conversation_id=replied.conversation_id,
                                                           expected_conversation_version=version(service, replied.conversation_id)))
    [cancelled] = follow_up_drafts(db, replied.lead_id)
    assert cancelled.status is OutboundStatus.CANCELLED
    transport = FakeEmailTransport()
    assert send(dispatcher(db, transport, clock=FrozenClock(LATER)), draft).outcome is DispatchOutcome.BLOCKED and transport.calls == []


def test_cancel_follow_up_and_stale_or_repeated_commands(db: Database) -> None:
    conversation_id, follow_up_id, _ = scheduled(db)
    service = operator(db)
    command = CancelFollowUp(command_id="cmd-c", correlation_id="c", conversation_id=conversation_id,
                             expected_conversation_version=version(service, conversation_id))
    service.cancel_follow_up(AS_ALICE, command)
    assert job(db, follow_up_id).status is FollowUpJobStatus.CANCELLED
    assert conversation(db, conversation_id).status is ConversationStatus.WAITING_FOR_REPLY
    assert service.cancel_follow_up(AS_ALICE, command).replayed  # identical replay
    with pytest.raises(StaleCommandError):
        service.cancel_follow_up(AS_ALICE, command.model_copy(update={"command_id": "cmd-c2"}))
    with pytest.raises(CommandRejectedError) as error:
        service.cancel_follow_up(AS_ALICE, CancelFollowUp(command_id="cmd-c3", correlation_id="c", conversation_id=conversation_id,
                                                          expected_conversation_version=version(service, conversation_id)))
    assert error.value.codes == (BlockCode.NO_FOLLOW_UP_TO_CANCEL,)


def test_close_conversation_keeps_the_lead_stage(db: Database) -> None:
    conversation_id, follow_up_id, lead_id = scheduled(db)
    service = operator(db)
    stage_before = service.get_lead(AS_ALICE, lead_id).stage
    service.close_conversation(AS_ALICE, CloseConversation(command_id="cmd-x", correlation_id="c", conversation_id=conversation_id,
                                                           expected_conversation_version=version(service, conversation_id)))
    view = service.get_conversation(AS_ALICE, conversation_id)
    assert view.status is ConversationStatus.CLOSED and view.lead_stage is stage_before
    assert job(db, follow_up_id).status is FollowUpJobStatus.CANCELLED


def test_operator_mark_do_not_contact_suppresses_and_cancels_everything_pending(db: Database) -> None:
    replied = replied_conversation(db)
    _, draft = follow_up_to_draft(db, replied.conversation_id)
    service = operator(db, FrozenClock(LATER))
    service.mark_do_not_contact(AS_ALICE, MarkDoNotContact(command_id="cmd-dnc", correlation_id="c", conversation_id=replied.conversation_id,
                                                           expected_conversation_version=version(service, replied.conversation_id)))
    assert conversation(db, replied.conversation_id).status is ConversationStatus.DO_NOT_CONTACT
    with db.transaction() as uow:
        assert len(uow.dnc.list_active(DNCScope.EMAIL, SENDER, LATER)) == 1
        cancelled = uow.outbound.get(draft)
    assert cancelled is not None and cancelled.status is OutboundStatus.CANCELLED
    with pytest.raises(CommandRejectedError):
        service.resume_conversation(AS_ALICE, ResumeConversation(command_id="cmd-r", correlation_id="c", conversation_id=replied.conversation_id,
                                                                 expected_conversation_version=version(service, replied.conversation_id)))


def test_rejected_follow_up_draft_returns_the_conversation_to_waiting(db: Database) -> None:
    replied = replied_conversation(db)
    _, draft = follow_up_to_draft(db, replied.conversation_id)
    service = operator(db, FrozenClock(LATER))
    service.reject_draft(AS_ALICE, reject_command(service.get_draft(AS_ALICE, draft), "cmd-rej"))
    assert conversation(db, replied.conversation_id).status is ConversationStatus.WAITING_FOR_REPLY
