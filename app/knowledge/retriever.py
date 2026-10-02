"""Knowledge retrieval services: what a workflow calls to get evidence + the gate verdict.

``LexicalRetriever``: the Stage 4 path (no embeddings, no network). Used whenever no
embeddings provider is configured.

``SemanticRetriever`` (Stage 19): the same gate over evidence ranked by embeddings.
1. A short transaction checks whether the configured space has any stored vector at all;
   if not, no query is embedded (nothing to search, no billable call) and the result is the
   gate over no evidence.
2. Otherwise the query's questions (already extracted, normalized and bounded by the
   caller's query builder; nothing else, e.g. no mailbox history) are embedded in ONE
   provider call, outside any database transaction.
3. A second short transaction re-selects usable sources and searches (``evaluate_knowledge``
   with ``SemanticSearch``).
A failed query embedding, or an index that cannot be searched safely, raises
``KnowledgeRetrievalError`` with a stable code: callers fail closed (no evidence is ever
invented, the lexical path is NOT silently substituted, the LLM is never asked to answer
from its own knowledge). Concurrent retrievals may embed the same question twice; that is
a cost, never a state change.
"""

import logging
import time
from datetime import datetime
from typing import Protocol

from app.core.models import KnowledgeQuery
from app.embeddings import EmbeddingError, EmbeddingErrorCode, EmbeddingPurpose, EmbeddingTransport
from app.knowledge.errors import KnowledgeError
from app.knowledge.models import KnowledgeResult
from app.knowledge.semantic import SemanticIndexError, SemanticSearch, space_key
from app.knowledge.service import evaluate_knowledge
from app.persistence import Database

LOG = logging.getLogger("app.knowledge.retrieval")
DEFAULT_MIN_SIMILARITY = 0.30


class KnowledgeRetrievalError(KnowledgeError):
    """Retrieval failed; ``code`` is a stable EmbeddingErrorCode value, never provider text."""

    def __init__(self, code: EmbeddingErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


class KnowledgeRetriever(Protocol):
    def evaluate(self, query: KnowledgeQuery, now: datetime) -> KnowledgeResult: ...


class LexicalRetriever:
    def __init__(self, db: Database) -> None:
        self._db = db

    def evaluate(self, query: KnowledgeQuery, now: datetime) -> KnowledgeResult:
        with self._db.transaction() as uow:
            return evaluate_knowledge(uow, query, now)


class SemanticRetriever:
    def __init__(self, db: Database, transport: EmbeddingTransport, *,
                 min_similarity: float = DEFAULT_MIN_SIMILARITY) -> None:
        if not 0.0 < min_similarity < 1.0:
            raise ValueError("min_similarity must be between 0 and 1 (exclusive)")
        self._db = db
        self._transport = transport
        self._min_similarity = min_similarity

    def __repr__(self) -> str:
        return f"SemanticRetriever(space={self._transport.space!r}, min_similarity={self._min_similarity})"

    def evaluate(self, query: KnowledgeQuery, now: datetime) -> KnowledgeResult:
        space = self._transport.space
        started = time.monotonic()
        with self._db.transaction() as uow:
            indexed = uow.knowledge_embeddings.count_space(space_key(space))
        vectors = {}
        if indexed:
            questions = list(query.questions)
            try:
                result = self._transport.embed(questions, EmbeddingPurpose.QUERY)
                if result.space != space or len(result.vectors) != len(questions):
                    raise EmbeddingError(EmbeddingErrorCode.INVALID_RESPONSE)
            except EmbeddingError as exc:
                self._log(query, exc.code.value, started, 0)
                raise KnowledgeRetrievalError(exc.code) from None
            vectors = dict(zip(questions, result.vectors, strict=True))
        search = SemanticSearch(space=space, query_vectors=vectors, min_similarity=self._min_similarity)
        try:
            with self._db.transaction() as uow:
                knowledge = evaluate_knowledge(uow, query, now, semantic=search)
        except SemanticIndexError as exc:
            self._log(query, exc.code.value, started, 0)
            raise KnowledgeRetrievalError(exc.code) from None
        self._log(query, "OK", started, len(knowledge.evidence))
        return knowledge

    def _log(self, query: KnowledgeQuery, outcome: str, started: float, hits: int) -> None:
        space = self._transport.space
        LOG.info("knowledge_retrieval method=SEMANTIC provider=%s model=%s query_id=%s questions=%d outcome=%s hits=%d "
                 "latency_ms=%d", space.provider, space.model, query.query_id, len(query.questions), outcome, hits,
                 int((time.monotonic() - started) * 1000))
