"""Initial V1 inbound reply safety mode.

AUTO_REPLY is disabled: answerable messages go to operator review, and risky or
unanswerable ones are escalated. This helper can never return AUTO_REPLY.
"""

from collections.abc import Mapping

from app.core.enums import KnowledgeDecision, ReplyDecision

_INITIAL_DECISION_BY_KNOWLEDGE: Mapping[KnowledgeDecision, ReplyDecision] = {
    KnowledgeDecision.SUFFICIENT: ReplyDecision.DRAFT_FOR_REVIEW,
    KnowledgeDecision.PARTIAL: ReplyDecision.DRAFT_FOR_REVIEW,
    KnowledgeDecision.INSUFFICIENT: ReplyDecision.ESCALATE,
    KnowledgeDecision.CONFLICTING: ReplyDecision.ESCALATE,
    KnowledgeDecision.STALE: ReplyDecision.ESCALATE,
    KnowledgeDecision.NOT_APPROVED: ReplyDecision.ESCALATE,
}


def resolve_initial_reply_decision(
    knowledge_decision: KnowledgeDecision,
    *,
    hard_escalation: bool = False,
    prefilter_no_action: bool = False,
) -> ReplyDecision:
    """Resolve the reply decision in the initial V1 mode.

    Precedence: a pre-filter NO_ACTION (bounce, auto-reply, duplicate, ...), then a
    hard escalation, then the knowledge decision.
    """
    if prefilter_no_action:
        return ReplyDecision.NO_ACTION
    if hard_escalation:
        return ReplyDecision.ESCALATE
    return _INITIAL_DECISION_BY_KNOWLEDGE[knowledge_decision]
