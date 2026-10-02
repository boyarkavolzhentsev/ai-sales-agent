"""Semantic (embedding) retrieval over the approved local knowledge index (Stage 19).

Same authority and the same contract as lexical retrieval (``app.knowledge.retrieval``);
only the *ranking* differs. Metadata filtering still happens first and ineligible content
has zero effect on anything:

1. The usable source versions are selected by metadata exactly as for lexical retrieval
   (approved, external use, current, locale, newest usable, not superseded).
2. The candidates are the chunks of those versions that have a stored vector in the
   configured embedding space (provider + exact model + requested dimensionality) whose
   ``input_hash`` equals the hash of the chunk's current embedding input. A vector of
   another space, of other text, or of a no-longer-usable source is never compared.
3. Per question: cosine similarity (dot product of unit vectors, rounded to
   ``SCORE_DECIMALS``) against every candidate; candidates below ``min_similarity`` are
   dropped (never "take the top k regardless"); sort by (score desc, chunk_id); keep the
   question's best ``top_k`` (never more than ``MAX_TOP_K``, whatever the query asks).
4. Merge questions (a chunk keeps its best score), sort the same way, keep evidence while
   the total excerpt size stays within ``MAX_EVIDENCE_CHARS``, assign ranks. Evidence IDs
   are ``evidence_id_for(query_id, chunk_id)``: the IDs the LLM contracts and the claim
   check already accept.

A similarity score is a *relevance* signal, never permission to answer: the deterministic
knowledge gate (lexical coverage, required domains, diagnostics, fact conflicts) still
decides sufficiency over whatever evidence is returned. A stored vector whose size differs
from the query vector fails the whole retrieval (DIMENSION_MISMATCH): nothing is mixed.

The embedding input of a chunk is exactly its stored text (it already starts with its
authoritative context line: heading path or document title). Nothing else is appended.
"""

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime

from app.core.enums import KnowledgeDomain
from app.core.models import KnowledgeChunk, KnowledgeEvidence, KnowledgeQuery, KnowledgeSource
from app.embeddings import EmbeddingError, EmbeddingErrorCode, EmbeddingSpace, Vector, decode, similarity
from app.knowledge.metadata import select_sources
from app.knowledge.retrieval import evidence_id_for
from app.persistence import UnitOfWork

# Bounds of what one retrieval passes on (and so of what reaches a prompt).
MAX_TOP_K = 20
MAX_EVIDENCE_CHARS = 24_000
# A chunk longer than this is never embedded (and never truncated): the indexer reports it.
MAX_EMBEDDING_INPUT_CHARS = 6_000


def embedding_input(chunk: KnowledgeChunk) -> str:
    return chunk.text


def input_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def space_key(space: EmbeddingSpace) -> tuple[str, str, int]:
    return (space.provider, space.model, space.requested_dimensions)


def all_versions(uow: UnitOfWork) -> list[KnowledgeSource]:
    versions: list[KnowledgeSource] = []
    for domain in KnowledgeDomain:
        versions.extend(uow.knowledge_sources.list_by_domain(domain))
    return versions


def indexable_sources(versions: Iterable[KnowledgeSource], now: datetime) -> tuple[KnowledgeSource, ...]:
    """The versions some query could use now: usable by ``select_sources`` for at least one
    source locale. Drafts, retired/withdrawn, internal-only, stale, not-yet-effective and
    superseded versions are never indexed."""
    versions = list(versions)
    usable: dict[tuple[str, int], KnowledgeSource] = {}
    for locale in sorted({s.locale.split("-")[0].casefold() for s in versions}):
        for source in select_sources(versions, now, locale).usable:
            usable[(source.source_id, source.version)] = source
    return tuple(usable[key] for key in sorted(usable))


class SemanticIndexError(Exception):
    """The stored index cannot be searched safely (e.g. mixed dimensionality)."""

    def __init__(self, code: EmbeddingErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True)
class SemanticSearch:
    """One retrieval's inputs: the space, each question's (unit) vector and the threshold.
    Questions without a vector (none were embedded) match nothing."""

    space: EmbeddingSpace
    query_vectors: Mapping[str, Vector]
    min_similarity: float

    def __repr__(self) -> str:  # never dump vectors
        return f"SemanticSearch(space={self.space!r}, questions={len(self.query_vectors)}, min={self.min_similarity})"


@dataclass(frozen=True)
class SemanticOutcome:
    evidence: tuple[KnowledgeEvidence, ...]
    eligible_chunks: int  # chunks of the usable sources
    searchable_chunks: int  # of those, with a current vector in this space
    dimensions: int | None


def searchable_vectors(uow: UnitOfWork, chunks: Iterable[KnowledgeChunk],
                       space: EmbeddingSpace) -> dict[str, Vector]:
    """chunk_id -> stored unit vector, only for the given chunks and only where the vector
    was made from the chunk's current embedding input in this exact space."""
    expected = {chunk.chunk_id: input_hash(embedding_input(chunk)) for chunk in chunks}
    vectors: dict[str, Vector] = {}
    sizes: set[int] = set()
    for record in uow.knowledge_embeddings.list_space(space_key(space)):
        if expected.get(record.key.chunk_id) != record.key.input_hash:
            continue
        try:
            vectors[record.key.chunk_id] = decode(record.vector, record.dimensions)
        except EmbeddingError as exc:
            raise SemanticIndexError(exc.code) from None
        sizes.add(record.dimensions)
    if len(sizes) > 1:
        raise SemanticIndexError(EmbeddingErrorCode.DIMENSION_MISMATCH)
    return vectors


def semantic_retrieve_from(uow: UnitOfWork, query: KnowledgeQuery, usable: Iterable[KnowledgeSource],
                           search: SemanticSearch) -> SemanticOutcome:
    by_key = {(s.source_id, s.version): s for s in usable}
    chunks = uow.knowledge_index.list_chunks_for_sources(by_key) if by_key else []
    vectors = searchable_vectors(uow, chunks, search.space)
    dimensions = len(next(iter(vectors.values()))) if vectors else None
    by_id = {chunk.chunk_id: chunk for chunk in chunks if chunk.chunk_id in vectors}

    best: dict[str, tuple[float, KnowledgeChunk]] = {}
    for question in query.questions:
        query_vector = search.query_vectors.get(question)
        if query_vector is None or not by_id:
            continue
        if dimensions is not None and len(query_vector) != dimensions:
            raise SemanticIndexError(EmbeddingErrorCode.DIMENSION_MISMATCH)
        scored = [(similarity(query_vector, vectors[chunk_id]), chunk) for chunk_id, chunk in by_id.items()]
        kept = sorted((item for item in scored if item[0] >= search.min_similarity),
                      key=lambda item: (-item[0], item[1].chunk_id))
        for score, chunk in kept[: min(query.top_k, MAX_TOP_K)]:
            current = best.get(chunk.chunk_id)
            if current is None or score > current[0]:
                best[chunk.chunk_id] = (score, chunk)

    evidence: list[KnowledgeEvidence] = []
    total = 0
    for score, chunk in sorted(best.values(), key=lambda item: (-item[0], item[1].chunk_id)):
        if total + len(chunk.text) > MAX_EVIDENCE_CHARS:
            break  # deterministic: the next item in rank order would exceed the bound
        total += len(chunk.text)
        source = by_key[(chunk.source_id, chunk.source_version)]
        evidence.append(KnowledgeEvidence(
            evidence_id=evidence_id_for(query.query_id, chunk.chunk_id), query_id=query.query_id,
            chunk_id=chunk.chunk_id, source_id=source.source_id, source_version=source.version, domain=source.domain,
            excerpt=chunk.text, score=score, rank=len(evidence) + 1, approval_status=source.approval_status,
            external_use=source.external_use, review_by=source.review_by,
        ))
    return SemanticOutcome(evidence=tuple(evidence), eligible_chunks=len(chunks), searchable_chunks=len(vectors),
                           dimensions=dimensions)
