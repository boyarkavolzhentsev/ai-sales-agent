"""Deterministic knowledge gate. Pure: no I/O, no clock, no LLM.

Per question (lexical coverage; see app.knowledge.scoring), first match wins:
1. usable evidence covers it (>= COVERAGE_THRESHOLD of its terms):
   CONFLICTING if any covering chunk states a conflicting structured fact, else SUFFICIENT;
2. an unapproved / internal-only / withdrawn source covers it: NOT_APPROVED;
3. only a stale source covers it: STALE;
4. usable evidence covers it partially (>= PARTIAL_MIN_TERMS terms and
   >= PARTIAL_MIN_FRACTION, below the threshold): PARTIAL;
5. otherwise (including a question with no searchable terms): INSUFFICIENT.
Diagnostics outrank weak partial matches, so an incidental shared word in usable
content can never hide the fact that the real answer is unapproved or stale.

Per required domain: covered when evidence *from that domain* covers some question;
otherwise NOT_APPROVED / STALE by the diagnostic rule, then PARTIAL for a partial
match, else INSUFFICIENT. Evidence from other domains never satisfies a required domain.

Overall decision = the worst of all per-question and per-domain decisions (severity:
NOT_APPROVED > CONFLICTING > STALE > INSUFFICIENT > PARTIAL > SUFFICIENT), so one
unanswerable question makes the whole query INSUFFICIENT. ``llm_sufficiency_opinion``
is always None here; a later LLM opinion may only downgrade (the KnowledgeAssessment
contract enforces this).
"""

from collections.abc import Mapping, Sequence

from app.core.decisions import combine_knowledge_decisions
from app.core.enums import KnowledgeDecision, KnowledgeDomain
from app.core.models import KnowledgeAssessment, KnowledgeEvidence, KnowledgeQuery, QuestionAssessment
from app.knowledge.models import DiagnosticHit, SourceUsability
from app.knowledge.scoring import COVERAGE_THRESHOLD, content_terms, coverage

_NOT_APPROVED_REASONS = frozenset(
    {SourceUsability.NOT_APPROVED, SourceUsability.INTERNAL_ONLY, SourceUsability.WITHDRAWN}
)


def _diagnostic_decision(hits: Sequence[DiagnosticHit]) -> KnowledgeDecision:
    covering = [hit for hit in hits if hit.coverage >= COVERAGE_THRESHOLD]
    if any(hit.usability in _NOT_APPROVED_REASONS for hit in covering):
        return KnowledgeDecision.NOT_APPROVED
    if any(hit.usability is SourceUsability.STALE for hit in covering):
        return KnowledgeDecision.STALE
    return KnowledgeDecision.INSUFFICIENT


def assess(
    query: KnowledgeQuery,
    evidence: Sequence[KnowledgeEvidence],
    *,
    diagnostics: Sequence[DiagnosticHit] = (),
    conflicting_chunks: Mapping[str, str] | None = None,
) -> KnowledgeAssessment:
    conflicts = conflicting_chunks or {}
    flags: list[str] = []
    reasons: list[str] = []
    per_question: list[QuestionAssessment] = []
    # 2 = covers some question, 1 = partial match, 0 = incidental or none.
    level_by_domain: dict[KnowledgeDomain, int] = {}

    if not evidence:
        flags.append("NO_USABLE_EVIDENCE")

    for index, question in enumerate(query.questions, start=1):
        terms = content_terms(question)
        covering: list[KnowledgeEvidence] = []
        partial: list[KnowledgeEvidence] = []
        for item in evidence:
            result = coverage(terms, item.excerpt)
            level = 2 if result.covers else 1 if result.is_partial else 0
            level_by_domain[item.domain] = max(level_by_domain.get(item.domain, 0), level)
            if result.covers:
                covering.append(item)
            elif result.is_partial:
                partial.append(item)

        if not terms:
            decision, cited = KnowledgeDecision.INSUFFICIENT, []
            flags.append(f"QUESTION_WITHOUT_TERMS:{index}")
        elif covering:
            conflicted = sorted({conflicts[e.chunk_id] for e in covering if e.chunk_id in conflicts})
            decision = KnowledgeDecision.CONFLICTING if conflicted else KnowledgeDecision.SUFFICIENT
            flags.extend(f"FACT_CONFLICT:{key}" for key in conflicted if f"FACT_CONFLICT:{key}" not in flags)
            cited = covering
        else:
            decision = _diagnostic_decision([h for h in diagnostics if h.question == question])
            cited = []
            if decision is KnowledgeDecision.INSUFFICIENT and partial:
                decision, cited = KnowledgeDecision.PARTIAL, partial
        if decision is not KnowledgeDecision.SUFFICIENT:
            reasons.append(f"question {index}: {decision}")
        per_question.append(
            QuestionAssessment(
                question=question,
                decision=decision,
                evidence_ids=tuple(e.evidence_id for e in cited),
            )
        )

    domain_decisions: list[KnowledgeDecision] = []
    for domain in query.required_domains:
        level = level_by_domain.get(domain, 0)
        if level == 2:
            continue
        decision = _diagnostic_decision([h for h in diagnostics if h.domain is domain])
        if decision is KnowledgeDecision.INSUFFICIENT and level == 1:
            decision = KnowledgeDecision.PARTIAL
        domain_decisions.append(decision)
        flags.append(f"REQUIRED_DOMAIN_{'PARTIAL' if level == 1 else 'MISSING'}:{domain}")
        reasons.append(f"required domain {domain}: {decision}")

    overall = combine_knowledge_decisions([q.decision for q in per_question] + domain_decisions)
    return KnowledgeAssessment(
        query_id=query.query_id,
        decision=overall,
        per_question=tuple(per_question),
        deterministic_flags=tuple(flags),
        llm_sufficiency_opinion=None,
        reasons=tuple(reasons),
    )
