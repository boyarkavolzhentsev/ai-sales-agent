"""Conversation creation, thread linking, idempotency, ambiguity and status/stage separation."""

from datetime import timedelta

from app.core.enums import CloseReason, ConversationStatus, LeadIntent, LeadStage
from app.conversation import FollowUpBlock, ScheduleOutcome, conversation_id_for
from app.llm import LLMTask
from app.persistence import Database
from tests.conversation.builders import conversation, customer_writes, replied_conversation, scheduler
from tests.inbound.builders import NOW, ScriptedTransport, classification, envelope, happy_transport, process
from tests.inbound.test_threads_and_transitions import outbound_history


def test_first_inbound_creates_an_active_conversation(db: Database) -> None:
    result = process(db, happy_transport(), envelope("p-1"))
    created = conversation(db, conversation_id_for(result.thread_id))
    assert (created.status, created.association_certain, created.thread_id) == (ConversationStatus.ACTIVE, True, result.thread_id)
    assert (created.last_inbound_message_id, created.last_inbound_at, created.follow_up_count) == (result.message_id, NOW, 0)


def test_subsequent_inbound_links_to_the_same_conversation(db: Database) -> None:
    replied = replied_conversation(db)
    before = conversation(db, replied.conversation_id)
    customer_writes(db, "p-2", received_at=NOW + timedelta(hours=2))
    after = conversation(db, replied.conversation_id)
    assert after.conversation_id == before.conversation_id and after.version > before.version
    assert after.status is ConversationStatus.OPERATOR_REVIEW  # the negotiation message was escalated
    assert after.last_inbound_at == NOW + timedelta(hours=2)


def test_duplicate_inbound_delivery_is_idempotent(db: Database) -> None:
    replied = replied_conversation(db)
    customer_writes(db, "p-2", received_at=NOW + timedelta(hours=2))
    first = conversation(db, replied.conversation_id)
    customer_writes(db, "p-2", received_at=NOW + timedelta(hours=2))  # same provider message again
    assert conversation(db, replied.conversation_id) == first


def test_out_of_order_message_does_not_replace_the_latest(db: Database) -> None:
    replied = replied_conversation(db)
    customer_writes(db, "p-3", received_at=NOW + timedelta(hours=3))
    latest = conversation(db, replied.conversation_id).last_inbound_message_id
    customer_writes(db, "p-2", received_at=NOW + timedelta(hours=1))  # older, delivered later
    after = conversation(db, replied.conversation_id)
    assert after.last_inbound_message_id == latest and after.last_inbound_at == NOW + timedelta(hours=3)
    assert after.last_activity_at == NOW + timedelta(hours=3)


def test_unverified_reference_never_merges_into_another_conversation(db: Database) -> None:
    replied = replied_conversation(db)
    before = conversation(db, replied.conversation_id)
    stranger = process(db, ScriptedTransport(), envelope("p-x", sender="stranger@elsewhere.example", in_reply_to="<p-1@prospect.example>"))
    theirs = conversation(db, conversation_id_for(stranger.thread_id))
    assert theirs.conversation_id != replied.conversation_id
    assert (theirs.association_certain, theirs.status) == (False, ConversationStatus.OPERATOR_REVIEW)
    assert conversation(db, replied.conversation_id) == before


def test_ambiguous_thread_association_requires_an_operator(db: Database) -> None:
    outbound_history(db, thread_id="th-a", rfc="<a@ourco.example>")
    outbound_history(db, thread_id="th-b", rfc="<b@ourco.example>")
    result = process(db, ScriptedTransport(), envelope("p-amb", references=("<a@ourco.example>", "<b@ourco.example>")))
    uncertain = conversation(db, conversation_id_for(result.thread_id))
    assert result.thread_id not in ("th-a", "th-b") and not uncertain.association_certain
    schedule = scheduler(db).schedule(uncertain.conversation_id, correlation_id="c")
    assert schedule.outcome is ScheduleOutcome.BLOCKED and FollowUpBlock.ASSOCIATION_UNCERTAIN in schedule.reason_codes


def test_lead_stage_and_conversation_status_stay_distinct(db: Database) -> None:
    replied = replied_conversation(db)
    with db.transaction() as uow:
        lead = uow.leads.get(replied.lead_id)
    assert lead is not None and lead.stage is LeadStage.INTERESTED
    assert conversation(db, replied.conversation_id).status is ConversationStatus.WAITING_FOR_REPLY
    assert "stage" not in conversation(db, replied.conversation_id).model_dump()


def test_unsubscribe_marks_every_conversation_of_the_contact_do_not_contact(db: Database) -> None:
    replied = replied_conversation(db)
    process(db, ScriptedTransport().script(LLMTask.INTENT_CLASSIFICATION, classification(LeadIntent.UNSUBSCRIBE)),
            envelope("p-unsub", body="Please unsubscribe me.", in_reply_to="<p-1@prospect.example>"))
    assert conversation(db, replied.conversation_id).status is ConversationStatus.DO_NOT_CONTACT


def test_lead_closure_closes_the_conversation(db: Database) -> None:
    replied = replied_conversation(db)
    process(db, ScriptedTransport().script(LLMTask.INTENT_CLASSIFICATION, classification(LeadIntent.NOT_INTERESTED)),
            envelope("p-no", body="Not interested, thanks.", in_reply_to="<p-1@prospect.example>"))
    with db.transaction() as uow:
        lead = uow.leads.get(replied.lead_id)
    assert lead is not None and lead.close_reason is CloseReason.NOT_INTERESTED
    assert conversation(db, replied.conversation_id).status is ConversationStatus.CLOSED
