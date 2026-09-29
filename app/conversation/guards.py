"""Dispatch-time guard for follow-up drafts (consulted by Stage 8 at claim time).

A follow-up draft that a human approved is still stopped at dispatch when its
conversation was paused, closed or suppressed, association became uncertain, the lead
closed or is on hold, an escalation is open, or another message of the lead has an
unresolved dispatch, a pending draft, or late-acceptance conflict evidence. An operator-
owned lead does not block: a human approved this specific message (Stage 7 semantics)."""

from datetime import datetime

from app.core.models import OutboundMessage
from app.conversation.policy import FollowUpBlock, common_blockers, load_facts
from app.conversation.state import is_follow_up
from app.persistence import UnitOfWork


def follow_up_dispatch_blockers(uow: UnitOfWork, outbound: OutboundMessage, now: datetime) -> list[str]:
    if not is_follow_up(outbound) or outbound.thread_id is None:
        return []
    conversation = uow.conversations.get_by_thread(outbound.thread_id)
    if conversation is None:
        return [FollowUpBlock.CONVERSATION_CLOSED]
    codes = common_blockers(load_facts(uow, conversation), now, own_outbound_id=outbound.outbound_id)
    return [code for code in codes if code != FollowUpBlock.LEAD_OPERATOR_OWNED]
