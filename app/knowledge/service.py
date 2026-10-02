from datetime import datetime

from app.core.models import KnowledgeQuery
from app.knowledge.gate import assess
from app.knowledge.models import KnowledgeResult, RetrievalInfo, RetrievalMethod
from app.knowledge.retrieval import conflicting_fact_chunks, diagnose, retrieve_from, select_for_query
from app.knowledge.semantic import SemanticSearch, semantic_retrieve_from
from app.persistence import UnitOfWork

INDEX_INCOMPLETE_FLAG = "SEMANTIC_INDEX_INCOMPLETE"


def evaluate_knowledge(uow: UnitOfWork, query: KnowledgeQuery, now: datetime, *,
                       semantic: SemanticSearch | None = None) -> KnowledgeResult:
    """Retrieve usable evidence and run the deterministic gate for one query.

    ``semantic`` selects evidence by stored embeddings instead of lexical BM25 ranking
    (Stage 19). Either way the same usable sources, the same evidence IDs and the same gate
    (lexical coverage, required domains, diagnostics, fact conflicts) apply: a similarity
    score never makes an answer sufficient on its own. With semantic retrieval, usable
    chunks that have no current vector are reported with the ``SEMANTIC_INDEX_INCOMPLETE``
    flag (run ``knowledge-index``); they can only make the result less complete, never less
    safe."""
    selection = select_for_query(uow, query, now)
    if semantic is None:
        evidence = retrieve_from(uow, query, selection.usable)
        retrieval = RetrievalInfo(method=RetrievalMethod.LEXICAL)
    else:
        outcome = semantic_retrieve_from(uow, query, selection.usable, semantic)
        evidence = outcome.evidence
        retrieval = RetrievalInfo(method=RetrievalMethod.SEMANTIC, provider=semantic.space.provider,
                                  model=semantic.space.model, dimensions=outcome.dimensions,
                                  min_similarity=semantic.min_similarity, eligible_chunks=outcome.eligible_chunks,
                                  searchable_chunks=outcome.searchable_chunks)
    assessment = assess(
        query,
        evidence,
        diagnostics=diagnose(uow, query, selection),
        conflicting_chunks=conflicting_fact_chunks(uow, selection.usable),
    )
    if retrieval.searchable_chunks < retrieval.eligible_chunks:
        flags = (*assessment.deterministic_flags, INDEX_INCOMPLETE_FLAG)
        assessment = type(assessment).model_validate(assessment.model_dump() | {"deterministic_flags": flags})
    return KnowledgeResult(evidence=evidence, assessment=assessment, retrieval=retrieval)
