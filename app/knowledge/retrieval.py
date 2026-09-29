"""Deterministic lexical retrieval over the local FTS5 index.

Metadata filtering happens before ranking, and ineligible content has zero effect on
candidates, scores, ranks, tie-breaks or top_k membership:

1. Load every version in ``allowed_domains``; select the usable ones by metadata
   (approved, external use, current, locale, newest usable, not superseded).
2. Build the eligible corpus: every chunk of those usable versions (and nothing else).
3. Per question: FTS5 MATCH, restricted inside SQL to the usable versions, returns all
   matching candidates. FTS5 is used only for matching; its own ranking is never used,
   because FTS5 statistics span the whole index including ineligible chunks.
4. Score each candidate with BM25 over the eligible corpus (see EligibleCorpus), sort by
   (score desc, chunk_id), keep the question's best ``top_k``.
5. Merge questions (a chunk keeps its best score), sort the same way, assign ranks.

``required_domains`` play no part here; they are coverage requirements checked by the
knowledge gate. ``top_k`` is per question, so one question cannot crowd another out; the
merged list holds at most ``top_k * len(questions)`` items.
"""

import hashlib
from collections.abc import Collection, Iterable, Mapping
from datetime import datetime

from app.core.models import KnowledgeChunk, KnowledgeEvidence, KnowledgeQuery, KnowledgeSource
from app.knowledge.metadata import SourceSelection, select_sources
from app.knowledge.models import DiagnosticHit, SourceUsability
from app.knowledge.scoring import EligibleCorpus, content_terms, coverage, fts_match_expression
from app.persistence import UnitOfWork

# Unusable sources that can explain why a question has no usable evidence.
DIAGNOSTIC_USABILITY: frozenset[SourceUsability] = frozenset(
    {
        SourceUsability.NOT_APPROVED,
        SourceUsability.INTERNAL_ONLY,
        SourceUsability.WITHDRAWN,
        SourceUsability.STALE,
    }
)


def evidence_id_for(query_id: str, chunk_id: str) -> str:
    return "ev_" + hashlib.sha256(f"{query_id}\n{chunk_id}".encode()).hexdigest()[:40]


def select_for_query(uow: UnitOfWork, query: KnowledgeQuery, now: datetime) -> SourceSelection:
    versions: list[KnowledgeSource] = []
    for domain in query.allowed_domains:
        versions.extend(uow.knowledge_sources.list_by_domain(domain))
    return select_sources(versions, now, query.locale)


def _keys(sources: Iterable[KnowledgeSource]) -> set[tuple[str, int]]:
    return {(s.source_id, s.version) for s in sources}


def _candidates(
    uow: UnitOfWork, terms: tuple[str, ...], sources: Collection[tuple[str, int]]
) -> list[KnowledgeChunk]:
    if not terms or not sources:
        return []
    return uow.knowledge_index.match_chunks(fts_match_expression(terms), sources)


def retrieve_from(
    uow: UnitOfWork, query: KnowledgeQuery, usable: Iterable[KnowledgeSource]
) -> tuple[KnowledgeEvidence, ...]:
    by_key = {(s.source_id, s.version): s for s in usable}
    if not by_key:
        return ()
    corpus = EligibleCorpus(chunk.text for chunk in uow.knowledge_index.list_chunks_for_sources(by_key))

    best: dict[str, tuple[float, KnowledgeChunk]] = {}
    for question in query.questions:
        terms = content_terms(question)
        scored = [(corpus.score(terms, chunk.text), chunk) for chunk in _candidates(uow, terms, by_key)]
        scored = sorted((item for item in scored if item[0] > 0), key=lambda item: (-item[0], item[1].chunk_id))
        for score, chunk in scored[: query.top_k]:
            current = best.get(chunk.chunk_id)
            if current is None or score > current[0]:
                best[chunk.chunk_id] = (score, chunk)

    ranked = sorted(best.values(), key=lambda item: (-item[0], item[1].chunk_id))
    evidence: list[KnowledgeEvidence] = []
    for rank, (score, chunk) in enumerate(ranked, start=1):
        source = by_key[(chunk.source_id, chunk.source_version)]
        evidence.append(
            KnowledgeEvidence(
                evidence_id=evidence_id_for(query.query_id, chunk.chunk_id),
                query_id=query.query_id,
                chunk_id=chunk.chunk_id,
                source_id=source.source_id,
                source_version=source.version,
                domain=source.domain,
                excerpt=chunk.text,
                score=score,
                rank=rank,
                approval_status=source.approval_status,
                external_use=source.external_use,
                review_by=source.review_by,
            )
        )
    return tuple(evidence)


def retrieve(uow: UnitOfWork, query: KnowledgeQuery, now: datetime) -> tuple[KnowledgeEvidence, ...]:
    """Evidence from usable sources only. ``now`` is explicit; nothing reads the clock."""
    return retrieve_from(uow, query, select_for_query(uow, query, now).usable)


def diagnose(
    uow: UnitOfWork, query: KnowledgeQuery, selection: SourceSelection
) -> tuple[DiagnosticHit, ...]:
    """Which unusable (unapproved, internal-only, withdrawn or stale) sources would have
    covered each question: the best coverage per question and source version. Runs
    separately from retrieval and never feeds evidence; text is inspected here and
    discarded, so hits carry none."""
    reasons = {
        (source.source_id, source.version): (source, usability)
        for source, usability in selection.excluded
        if usability in DIAGNOSTIC_USABILITY
    }
    hits: list[DiagnosticHit] = []
    for question in query.questions:
        terms = content_terms(question)
        best: dict[tuple[str, int], float] = {}
        for chunk in _candidates(uow, terms, reasons):
            key = (chunk.source_id, chunk.source_version)
            best[key] = max(best.get(key, 0.0), coverage(terms, chunk.text).fraction)
        for key in sorted(best):
            source, usability = reasons[key]
            hits.append(
                DiagnosticHit(
                    question=question,
                    source_id=source.source_id,
                    source_version=source.version,
                    domain=source.domain,
                    usability=usability,
                    coverage=best[key],
                )
            )
    return tuple(hits)


def conflicting_fact_chunks(
    uow: UnitOfWork, usable: Iterable[KnowledgeSource]
) -> Mapping[str, str]:
    """chunk_id -> fact_key for every fact whose key has more than one distinct value
    (compared case-folded, with its unit) across the usable sources."""
    facts = uow.knowledge_index.list_facts_for_sources(_keys(usable))
    values: dict[str, set[tuple[str, str]]] = {}
    for fact in facts:
        values.setdefault(fact.fact_key, set()).add(
            (fact.value.strip().casefold(), (fact.unit or "").strip().casefold())
        )
    conflicted = {key for key, distinct in values.items() if len(distinct) > 1}
    return {fact.chunk_id: fact.fact_key for fact in facts if fact.fact_key in conflicted}
