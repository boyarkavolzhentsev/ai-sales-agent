"""Smoke: enrollment -> first touch -> approval -> dispatch -> follow-up -> reply handoff."""

from app.core.enums import CampaignMemberStatus, ConversationStatus, FollowUpStatus, LeadIntent, LeadStage, OutboundKind
from app.conversation import conversation_id_for
from app.dispatch import DispatchOutcome
from app.llm import LLMTask
from app.persistence import Database
from tests.campaign.builders import INTERVAL, PROSPECT, draft_touch, member, outbound, ready_campaign, send_touch
from tests.inbound.builders import NOW, ScriptedTransport, classification, envelope, process


def test_full_campaign_cycle_hands_off_to_the_conversation(db: Database) -> None:
    member_id = ready_campaign(db)
    first = draft_touch(db)
    assert first.outbound_id is not None and member(db, member_id).status is CampaignMemberStatus.DRAFTED
    draft = outbound(db, first.outbound_id)
    assert draft.kind is OutboundKind.FIRST_TOUCH and "Lena" in draft.body_final and "spreadsheet exports" in draft.body_final

    assert send_touch(db, first.outbound_id).outcome is DispatchOutcome.ACCEPTED
    waiting = member(db, member_id)
    assert (waiting.status, waiting.touch_count) == (CampaignMemberStatus.WAITING, 1)
    with db.transaction() as uow:
        lead = uow.leads.get(waiting.lead_id or "")
        plan = uow.follow_ups.get_open_for_lead(waiting.lead_id or "")
    assert lead is not None and lead.stage is LeadStage.CONTACTED
    assert plan is not None and plan.status is FollowUpStatus.ACTIVE

    second = draft_touch(db, at=NOW + INTERVAL)
    assert second.outbound_id is not None and outbound(db, second.outbound_id).kind is OutboundKind.FOLLOW_UP
    assert send_touch(db, second.outbound_id, at=NOW + INTERVAL).outcome is DispatchOutcome.ACCEPTED
    assert member(db, member_id).touch_count == 2

    reply_to = outbound(db, second.outbound_id).rfc_message_id
    result = process(db, ScriptedTransport().script(LLMTask.INTENT_CLASSIFICATION, classification(LeadIntent.NEGOTIATION)),
                     envelope("p-r", sender=PROSPECT, body="Interesting, can we talk price?", in_reply_to=reply_to))
    replied = member(db, member_id)
    assert replied.status is CampaignMemberStatus.REPLIED and result.lead_id == replied.lead_id
    with db.transaction() as uow:
        conversation = uow.conversations.get(conversation_id_for(result.thread_id))
        plan = uow.follow_ups.get_open_for_lead(replied.lead_id or "")
    assert conversation is not None and conversation.status is not ConversationStatus.DO_NOT_CONTACT
    assert result.thread_id == replied.thread_id and plan is None
