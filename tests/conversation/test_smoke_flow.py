"""Smoke: the full Stage 9 loop across Stages 6-8."""

from app.core.enums import ConversationStatus, LeadStage
from app.dispatch import DispatchOutcome
from app.persistence import Database
from tests.conversation.builders import (
    FIRST_DUE,
    conversation,
    dispatch_follow_up,
    follow_up_to_draft,
    replied_conversation,
)
from tests.operator.builders import AS_ALICE, operator


def test_reply_then_follow_up_is_drafted_approved_and_dispatched(db: Database) -> None:
    replied = replied_conversation(db)
    waiting = conversation(db, replied.conversation_id)
    assert waiting.status is ConversationStatus.WAITING_FOR_REPLY and waiting.last_outbound_id == replied.reply_outbound_id
    view = operator(db).get_conversation(AS_ALICE, replied.conversation_id)
    assert view.lead_stage is LeadStage.INTERESTED and view.status is ConversationStatus.WAITING_FOR_REPLY

    job, draft = follow_up_to_draft(db, replied.conversation_id)
    assert conversation(db, replied.conversation_id).status is ConversationStatus.OPERATOR_REVIEW
    detail = operator(db).get_draft(AS_ALICE, draft)
    assert detail.actionable, detail.blockers
    result = dispatch_follow_up(db, draft, FIRST_DUE)
    assert result.outcome is DispatchOutcome.ACCEPTED
    after = conversation(db, replied.conversation_id)
    assert (after.status, after.follow_up_count, after.last_outbound_id) == (ConversationStatus.WAITING_FOR_REPLY, 1, draft)
