from datetime import datetime

from app.core.models import KnowledgeQuery
from app.knowledge.gate import assess
from app.knowledge.models import KnowledgeResult
from app.knowledge.retrieval import conflicting_fact_chunks, diagnose, retrieve_from, select_for_query
from app.persistence import UnitOfWork


def evaluate_knowledge(uow: UnitOfWork, query: KnowledgeQuery, now: datetime) -> KnowledgeResult:
    """Retrieve usable evidence and run the deterministic gate for one query."""
    selection = select_for_query(uow, query, now)
    evidence = retrieve_from(uow, query, selection.usable)
    assessment = assess(
        query,
        evidence,
        diagnostics=diagnose(uow, query, selection),
        conflicting_chunks=conflicting_fact_chunks(uow, selection.usable),
    )
    return KnowledgeResult(evidence=evidence, assessment=assessment)
