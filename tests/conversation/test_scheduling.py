"""Scheduling eligibility, duplicate prevention and policy blocks."""

from datetime import timedelta

from app.core.enums import (
    CloseReason,
    ConversationStatus,
    DNCReason,
    DNCScope,
    EscalationReason,
    FollowUpJobStatus,
    LeadStage,
    RefKind,
)
from app.core.models import DoNotContactEntry, EntityRef, Escalation
from app.conversation import FollowUpBlock, ScheduleOutcome, conversation_id_for
from app.dispatch import DispatchOutcome, FakeBehavior, FakeEmailTransport, FakeReconciler
from app.persistence import Database, FrozenClock
from tests.conversation.builders import (
    FIRST_DUE,
    SECOND_DUE_AFTER,
    SENT_AT,
    approve,
    conversation,
    dispatch_follow_up,
    follow_up_to_draft,
    jobs,
    replied_conversation,
    scheduler,
)
from app.operator import CloseConversation
from tests.dispatch.builders import dispatcher, request, send
from tests.inbound.builders import NOW, SENDER, envelope, happy_transport, process
from tests.operator.builders import AS_ALICE, operator, reject_command


def blocked(db: Database, conversation_id: str) -> set[str]:
    result = scheduler(db).schedule(conversation_id, correlation_id="corr-s")
    assert result.outcome is ScheduleOutcome.BLOCKED, result
    return set(result.reason_codes)


def test_eligible_conversation_schedules_one_follow_up(db: Database) -> None:
    replied = replied_conversation(db)
    result = scheduler(db).schedule(replied.conversation_id, correlation_id="corr-s")
    assert (result.outcome, result.due_at) == (ScheduleOutcome.SCHEDULED, SENT_AT + timedelta(days=3))
    after = conversation(db, replied.conversation_id)
    assert (after.status, after.next_follow_up_at) == (ConversationStatus.FOLLOW_UP_DUE, FIRST_DUE)
    [job] = jobs(db, replied.conversation_id)
    assert (job.status, job.sequence_no, job.anchor_outbound_id) == (FollowUpJobStatus.SCHEDULED, 1, replied.reply_outbound_id)


def test_scheduling_twice_is_idempotent(db: Database) -> None:
    replied = replied_conversation(db)
    first = scheduler(db).schedule(replied.conversation_id, correlation_id="corr-1")
    second = scheduler(db).schedule(replied.conversation_id, correlation_id="corr-2")
    assert second.outcome is ScheduleOutcome.ALREADY_SCHEDULED and second.follow_up_id == first.follow_up_id
    assert len(jobs(db, replied.conversation_id)) == 1


def test_an_executed_follow_up_is_never_scheduled_again(db: Database) -> None:
    replied = replied_conversation(db)
    job, draft = follow_up_to_draft(db, replied.conversation_id)
    service = operator(db, FrozenClock(FIRST_DUE))
    service.reject_draft(AS_ALICE, reject_command(service.get_draft(AS_ALICE, draft), "cmd-reject"))  # the operator declines it
    after = conversation(db, replied.conversation_id)
    assert after.status is ConversationStatus.WAITING_FOR_REPLY
    again = scheduler(db, FrozenClock(FIRST_DUE)).schedule(replied.conversation_id, correlation_id="corr-again")
    assert again.outcome is ScheduleOutcome.ALREADY_EXECUTED and again.follow_up_id == job.follow_up_id
    assert len(jobs(db, replied.conversation_id)) == 1


def test_max_follow_ups_is_respected(db: Database) -> None:
    replied = replied_conversation(db)
    _, first = follow_up_to_draft(db, replied.conversation_id)
    assert dispatch_follow_up(db, first, FIRST_DUE).outcome is DispatchOutcome.ACCEPTED
    second_due = FIRST_DUE + SECOND_DUE_AFTER
    job2, second = follow_up_to_draft(db, replied.conversation_id, due=second_due)
    assert job2.sequence_no == 2
    assert dispatch_follow_up(db, second, second_due).outcome is DispatchOutcome.ACCEPTED
    assert conversation(db, replied.conversation_id).follow_up_count == 2
    assert FollowUpBlock.MAX_FOLLOW_UPS in blocked(db, replied.conversation_id)


def test_dnc_blocks_scheduling(db: Database) -> None:
    replied = replied_conversation(db)
    with db.transaction() as uow:
        uow.dnc.add(DoNotContactEntry(entry_id="dnc-1", scope=DNCScope.EMAIL, value=SENDER, reason=DNCReason.OPERATOR,
                                      source_ref=EntityRef(kind=RefKind.OPERATOR_COMMAND, id="c"), created_by="op", created_at=NOW))
    assert FollowUpBlock.DO_NOT_CONTACT in blocked(db, replied.conversation_id)


def test_closed_or_converted_conversation_blocks_scheduling(db: Database) -> None:
    replied = replied_conversation(db)
    with db.transaction() as uow:
        lead = uow.leads.get(replied.lead_id)
        assert lead is not None
        uow.leads.update(lead.model_copy(update={"stage": LeadStage.CLOSED, "close_reason": CloseReason.WON, "version": lead.version + 1}), lead.version)
    assert FollowUpBlock.LEAD_CLOSED in blocked(db, replied.conversation_id)

    other = replied_conversation(db, "p-9", sender="other@elsewhere.example")
    service = operator(db)
    view = service.get_conversation(AS_ALICE, other.conversation_id)
    service.close_conversation(AS_ALICE, CloseConversation(command_id="cmd-close", correlation_id="c", conversation_id=other.conversation_id,
                                                          expected_conversation_version=view.version))
    assert FollowUpBlock.CONVERSATION_CLOSED in blocked(db, other.conversation_id)


def test_unresolved_operator_review_blocks_scheduling(db: Database) -> None:
    replied = replied_conversation(db)
    with db.transaction() as uow:
        uow.escalations.add(Escalation(escalation_id="es-1", lead_id=replied.lead_id, trigger_ref=EntityRef(kind=RefKind.LEAD, id=replied.lead_id),
                                       reasons=(EscalationReason.OPERATOR_REQUESTED,), created_at=NOW))
    assert FollowUpBlock.OPERATOR_REVIEW_OPEN in blocked(db, replied.conversation_id)


def test_unresolved_stage8_dispatch_blocks_until_reconciled(db: Database) -> None:
    result = process(db, happy_transport(), envelope("p-1"))
    assert result.outbound_id is not None
    approve(db, result.outbound_id, FrozenClock(NOW))
    transport = FakeEmailTransport().script(FakeBehavior.ACCEPT_THEN_LOSE_RESPONSE)
    assert send(dispatcher(db, transport), result.outbound_id).outcome is DispatchOutcome.UNKNOWN
    conversation_id = conversation_id_for(result.thread_id)
    assert FollowUpBlock.DISPATCH_UNRESOLVED in blocked(db, conversation_id)

    dispatcher(db, transport, reconciler=FakeReconciler(transport)).reconcile(request(result.outbound_id, "corr-rec"))
    assert conversation(db, conversation_id).status is ConversationStatus.WAITING_FOR_REPLY
    assert scheduler(db).schedule(conversation_id, correlation_id="corr-s").outcome is ScheduleOutcome.SCHEDULED


def test_a_conversation_awaiting_our_response_is_not_followed_up(db: Database) -> None:
    result = process(db, happy_transport(), envelope("p-1"))  # draft awaiting review: nothing sent yet
    assert {FollowUpBlock.CONVERSATION_NOT_WAITING, FollowUpBlock.OUTBOUND_PENDING} <= blocked(db, conversation_id_for(result.thread_id))
