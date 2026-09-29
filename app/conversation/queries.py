from app.conversation.models import ConversationView, FollowUpJobView
from app.persistence import UnitOfWork


def conversation_view(uow: UnitOfWork, conversation_id: str) -> ConversationView | None:
    conversation = uow.conversations.get(conversation_id)
    if conversation is None:
        return None
    lead = uow.leads.get(conversation.lead_id)
    jobs = uow.follow_up_jobs.list_for_conversation(conversation_id)
    return ConversationView(
        conversation_id=conversation.conversation_id, thread_id=conversation.thread_id, lead_id=conversation.lead_id,
        status=conversation.status, lead_stage=lead.stage if lead else None, lead_status=lead.status if lead else None,
        association_certain=conversation.association_certain, last_inbound_message_id=conversation.last_inbound_message_id,
        last_inbound_at=conversation.last_inbound_at, last_outbound_id=conversation.last_outbound_id,
        last_outbound_at=conversation.last_outbound_at, last_activity_at=conversation.last_activity_at,
        next_follow_up_at=conversation.next_follow_up_at, follow_up_count=conversation.follow_up_count,
        version=conversation.version,
        jobs=tuple(
            FollowUpJobView(follow_up_id=j.follow_up_id, sequence_no=j.sequence_no, status=j.status, due_at=j.due_at,
                            outbound_id=j.outbound_id, block_codes=j.block_codes)
            for j in jobs
        ),
    )
