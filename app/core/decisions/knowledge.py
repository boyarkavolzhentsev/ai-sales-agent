from collections.abc import Iterable

from app.core.enums import KnowledgeDecision

# Worst first. The combined decision for several questions is the worst one.
KNOWLEDGE_DECISION_SEVERITY: tuple[KnowledgeDecision, ...] = (
    KnowledgeDecision.NOT_APPROVED,
    KnowledgeDecision.CONFLICTING,
    KnowledgeDecision.STALE,
    KnowledgeDecision.INSUFFICIENT,
    KnowledgeDecision.PARTIAL,
    KnowledgeDecision.SUFFICIENT,
)

_SEVERITY_RANK: dict[KnowledgeDecision, int] = {
    decision: len(KNOWLEDGE_DECISION_SEVERITY) - index
    for index, decision in enumerate(KNOWLEDGE_DECISION_SEVERITY)
}


def knowledge_severity(decision: KnowledgeDecision) -> int:
    """Higher means worse. SUFFICIENT is 1, NOT_APPROVED is the maximum."""
    return _SEVERITY_RANK[decision]


def combine_knowledge_decisions(decisions: Iterable[KnowledgeDecision]) -> KnowledgeDecision:
    """Return the worst decision. No decisions means no evidence: INSUFFICIENT."""
    return max(decisions, key=knowledge_severity, default=KnowledgeDecision.INSUFFICIENT)
