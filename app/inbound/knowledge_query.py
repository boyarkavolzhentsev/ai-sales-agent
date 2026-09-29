"""Deterministic intent/question -> KnowledgeQuery routing. The LLM never chooses domains.

Each answerable intent has a fixed set of allowed domains and intent-level required
domains. Keywords in the (validated) questions add required domains: pricing words require
PRICING_COMMERCIAL, product words PRODUCTS_SERVICES, meeting words MEETING_GUIDANCE. A
required domain is always also allowed. Questions are normalized (whitespace collapsed,
case-insensitive duplicates merged). Questions over the length limit or beyond the count
cap are reported as omitted, never truncated or silently dropped. Classifier-extracted
questions do not guarantee complete semantic coverage of the email; the operator reviews
every draft in V1.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from app.core.enums import KnowledgeDomain, KnowledgePurpose, LeadIntent
from app.core.models import KnowledgeQuery
from app.inbound.models import stable_id
from app.knowledge.scoring import content_terms

D = KnowledgeDomain

ANSWERABLE_INTENTS: frozenset[LeadIntent] = frozenset(
    {
        LeadIntent.INFO_REQUEST,
        LeadIntent.PRICING_REQUEST,
        LeadIntent.MEETING_REQUEST,
        LeadIntent.POSITIVE_INTEREST,
        LeadIntent.OBJECTION,
    }
)

# intent -> (allowed domains, intent-level required domains)
INTENT_DOMAINS: Mapping[LeadIntent, tuple[tuple[KnowledgeDomain, ...], tuple[KnowledgeDomain, ...]]] = {
    LeadIntent.PRICING_REQUEST: ((D.PRICING_COMMERCIAL, D.PRODUCTS_SERVICES, D.FAQ, D.COMPANY), (D.PRICING_COMMERCIAL,)),
    LeadIntent.MEETING_REQUEST: ((D.MEETING_GUIDANCE, D.COMPANY, D.FAQ, D.PRODUCTS_SERVICES), (D.MEETING_GUIDANCE,)),
    LeadIntent.INFO_REQUEST: ((D.PRODUCTS_SERVICES, D.COMPANY, D.FAQ, D.MEETING_GUIDANCE), ()),
    LeadIntent.POSITIVE_INTEREST: ((D.PRODUCTS_SERVICES, D.COMPANY, D.FAQ, D.MEETING_GUIDANCE), ()),
    LeadIntent.OBJECTION: ((D.OBJECTIONS, D.PRODUCTS_SERVICES, D.FAQ, D.COMPANY), (D.OBJECTIONS,)),
}

KEYWORD_DOMAINS: Mapping[KnowledgeDomain, frozenset[str]] = {
    D.PRICING_COMMERCIAL: frozenset(
        {"price", "prices", "pricing", "cost", "costs", "fee", "fees", "plan", "plans", "discount",
         "discounts", "invoice", "invoices", "billing", "payment", "payments", "quote", "eur", "usd"}
    ),
    D.PRODUCTS_SERVICES: frozenset(
        {"product", "products", "feature", "features", "integrate", "integrates", "integration",
         "integrations", "api", "capability", "capabilities", "offline", "export", "exports", "service", "services"}
    ),
    D.MEETING_GUIDANCE: frozenset({"meeting", "meet", "call", "demo", "schedule", "book", "booking"}),
}


@dataclass(frozen=True)
class QuestionSet:
    """``kept`` are assessed. ``omitted`` exceeded a processing limit (length or count);
    they are never silently dropped: any omission makes the caller fail closed."""

    kept: tuple[str, ...]
    omitted: tuple[str, ...]


@dataclass(frozen=True)
class QueryPlan:
    query: KnowledgeQuery | None
    omitted: tuple[str, ...]


def normalize_questions(questions: Iterable[str], *, max_questions: int, max_chars: int) -> QuestionSet:
    """Collapse whitespace and merge case-insensitive duplicates (the only removals).
    Questions without searchable terms are kept: the knowledge gate assesses them as
    INSUFFICIENT rather than letting them vanish."""
    kept: dict[str, str] = {}
    omitted: dict[str, str] = {}
    for question in questions:
        text = " ".join(question.split())
        key = text.casefold()
        if not text or key in kept or key in omitted:
            continue
        if len(text) > max_chars:
            omitted[key] = text
        else:
            kept[key] = text
    ordered = list(kept.values())
    return QuestionSet(kept=tuple(ordered[:max_questions]), omitted=(*omitted.values(), *ordered[max_questions:]))


def route_domains(
    intent: LeadIntent, questions: Iterable[str]
) -> tuple[tuple[KnowledgeDomain, ...], tuple[KnowledgeDomain, ...]]:
    allowed, required = INTENT_DOMAINS[intent]
    terms = {term for question in questions for term in content_terms(question)}
    keyword_required = {domain for domain, words in KEYWORD_DOMAINS.items() if terms & words}
    required_set = set(required) | keyword_required
    allowed_set = set(allowed) | required_set
    order = list(KnowledgeDomain)
    return (
        tuple(sorted(allowed_set, key=order.index)),
        tuple(sorted(required_set, key=order.index)),
    )


def build_query(
    *,
    message_id: str,
    intent: LeadIntent,
    questions: Iterable[str],
    locale: str,
    top_k: int,
    correlation_id: str,
    max_questions: int,
    max_chars: int,
) -> QueryPlan:
    if intent not in ANSWERABLE_INTENTS:
        raise ValueError(f"{intent} is not answerable from knowledge")
    questions_set = normalize_questions(questions, max_questions=max_questions, max_chars=max_chars)
    if not questions_set.kept:
        return QueryPlan(query=None, omitted=questions_set.omitted)
    allowed, required = route_domains(intent, questions_set.kept)
    query = KnowledgeQuery(
        query_id=stable_id("kq", message_id),
        purpose=KnowledgePurpose.INBOUND_REPLY,
        questions=questions_set.kept,
        allowed_domains=allowed,
        required_domains=required,
        locale=locale,
        top_k=top_k,
        correlation_id=correlation_id,
    )
    return QueryPlan(query=query, omitted=questions_set.omitted)
