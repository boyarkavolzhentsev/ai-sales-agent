"""Conversation state and durable follow-ups (V1: deterministic, operator-gated).

A conversation is one email thread with one lead; its operational status is separate
from the lead's pipeline stage. Follow-ups are durable jobs whose identity is the
logical action ("follow-up N after outbound X"); executing one produces a reviewable
draft, which Stage 7 approval and Stage 8 dispatch handle like any reply.

Imports app.core, app.persistence, app.policy (suppression, reply policy, release) and
app.llm (claim check, sender identity) only. It never sends, calls providers or Telegram,
and never imports app.inbound, app.operator or app.dispatch (they depend on it).
"""

from app.conversation.executor import FollowUpExecutor
from app.conversation.ids import FOLLOW_UP_KEY_PREFIX, conversation_id_for, follow_up_id_for
from app.conversation.models import (
    ConversationView,
    ExecutionOutcome,
    ExecutionResult,
    FollowUpClaim,
    FollowUpJobView,
    ScheduleOutcome,
    ScheduleResult,
)
from app.conversation.policy import FollowUpBlock, FollowUpConfig
from app.conversation.scheduler import FollowUpScheduler
from app.conversation.worker import run_once

__all__ = [
    "FOLLOW_UP_KEY_PREFIX",
    "ConversationView",
    "ExecutionOutcome",
    "ExecutionResult",
    "FollowUpBlock",
    "FollowUpClaim",
    "FollowUpConfig",
    "FollowUpExecutor",
    "FollowUpJobView",
    "FollowUpScheduler",
    "ScheduleOutcome",
    "ScheduleResult",
    "conversation_id_for",
    "follow_up_id_for",
    "run_once",
]
