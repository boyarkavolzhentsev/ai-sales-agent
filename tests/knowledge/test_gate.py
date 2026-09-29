from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.decisions import combine_knowledge_decisions, knowledge_severity
from app.core.enums import KnowledgeDecision, KnowledgeDomain
from app.core.models import KnowledgeAssessment
from app.knowledge import DiagnosticHit, SourceUsability, assess, evaluate_knowledge
from app.knowledge.models import KnowledgeResult
from app.persistence import Database
from tests.knowledge.conftest import ingest
from tests.knowledge.sources import NOW, fact, meta, query, write, yaml_doc

D = KnowledgeDomain
K = KnowledgeDecision


def evaluate(db: Database, *questions: str, **kwargs: object) -> KnowledgeResult:
    with db.transaction() as uow:
        return evaluate_knowledge(uow, query(*questions, **kwargs), NOW)  # type: ignore[arg-type]


# ---- F. Gate decisions (fixture knowledge base) ----------------------------------------


def test_sufficient(fixture_db: Database) -> None:
    result = evaluate(fixture_db, "What does the Basic plan cost per month?", required=(D.PRICING_COMMERCIAL,))
    assessment = result.assessment
    assert assessment.decision is K.SUFFICIENT
    [question] = assessment.per_question
    assert question.decision is K.SUFFICIENT and question.evidence_ids
    assert assessment.llm_sufficiency_opinion is None


PARTIAL_QUESTION = 'Is the Team plan price negotiable for nonprofit schools in Sampletown?'


def test_partial(fixture_db: Database) -> None:
    # 3 of 7 terms (team, plan, price) are covered: more than incidental, below half.
    result = evaluate(fixture_db, PARTIAL_QUESTION)
    assert result.assessment.decision is K.PARTIAL
    assert result.assessment.per_question[0].evidence_ids


def test_single_shared_word_is_incidental_not_partial(fixture_db: Database) -> None:
    # Only "support" overlaps with usable content.
    result = evaluate(fixture_db, "Do you support blockchain tokens?")
    assert result.assessment.decision is K.INSUFFICIENT
    assert result.assessment.per_question[0].evidence_ids == ()


def test_no_stemming_errs_toward_insufficient(fixture_db: Database) -> None:
    # "take" does not match "takes": only "onboarding" overlaps, so no answer is claimed.
    assert evaluate(fixture_db, "How long does onboarding take?").assessment.decision is K.INSUFFICIENT


def test_insufficient(fixture_db: Database) -> None:
    result = evaluate(fixture_db, "Do you support blockchain tokens?")
    assert result.assessment.decision is K.INSUFFICIENT


@pytest.mark.parametrize(
    "question",
    [
        "Do custom contract discounts require legal sign-off?",  # INTERNAL_ONLY
        "What if the prospect says the widget is too expensive?",  # DRAFT
    ],
)
def test_not_approved(fixture_db: Database, question: str) -> None:
    # The draft question also shares the word "widget" with usable content; that
    # incidental overlap must not hide the unapproved source that actually answers it.
    result = evaluate(fixture_db, question)
    assert result.assessment.decision is K.NOT_APPROVED
    assert result.evidence == () or all(e.approval_status.value == "APPROVED" for e in result.evidence)


def test_stale(fixture_db: Database) -> None:
    result = evaluate(fixture_db, "How did the fictional retailer reduce manual reporting time?")
    assert result.assessment.decision is K.STALE
    assert all(e.source_id != "sample-case-study" for e in result.evidence)


def test_question_without_searchable_terms_is_insufficient(fixture_db: Database) -> None:
    result = evaluate(fixture_db, "Is it?")
    assert result.assessment.decision is K.INSUFFICIENT
    assert "QUESTION_WITHOUT_TERMS:1" in result.assessment.deterministic_flags


def test_one_unanswerable_question_makes_the_query_insufficient(fixture_db: Database) -> None:
    result = evaluate(fixture_db, "What does the Basic plan cost per month?", "Do you support blockchain tokens?")
    assert [q.decision for q in result.assessment.per_question] == [K.SUFFICIENT, K.INSUFFICIENT]
    assert result.assessment.decision is K.INSUFFICIENT


# ---- Required-domain coverage ----------------------------------------------------------


def test_faq_only_evidence_never_satisfies_required_pricing_and_products(fixture_db: Database) -> None:
    result = evaluate(
        fixture_db,
        "Does support answer on working days?",
        required=(D.PRICING_COMMERCIAL, D.PRODUCTS_SERVICES),
    )
    assessment = result.assessment
    [answer] = assessment.per_question
    assert answer.decision is K.SUFFICIENT  # the question itself is answered...
    covering = {e.domain for e in result.evidence if e.evidence_id in answer.evidence_ids}
    assert covering == {D.FAQ}  # ...only by FAQ evidence
    assert assessment.decision is not K.SUFFICIENT  # ...but the required domains are not
    assert assessment.decision is K.INSUFFICIENT
    assert {"REQUIRED_DOMAIN_MISSING:PRICING_COMMERCIAL", "REQUIRED_DOMAIN_MISSING:PRODUCTS_SERVICES"} <= set(
        assessment.deterministic_flags
    )


def test_every_required_domain_needs_its_own_evidence(fixture_db: Database) -> None:
    result = evaluate(
        fixture_db,
        "What does the Basic plan cost per month?",
        "Does the Sample Widget support offline mode?",
        required=(D.PRICING_COMMERCIAL, D.PRODUCTS_SERVICES),
    )
    assert result.assessment.decision is K.SUFFICIENT
    missing = evaluate(fixture_db, "What does the Basic plan cost per month?", required=(D.PRICING_COMMERCIAL, D.PRODUCTS_SERVICES))
    assert missing.assessment.decision is not K.SUFFICIENT
    assert "REQUIRED_DOMAIN_PARTIAL:PRODUCTS_SERVICES" in missing.assessment.deterministic_flags or (
        "REQUIRED_DOMAIN_MISSING:PRODUCTS_SERVICES" in missing.assessment.deterministic_flags
    )


def test_required_domain_covered_only_by_stale_source_is_stale(fixture_db: Database) -> None:
    result = evaluate(
        fixture_db, "How did the fictional retailer reduce manual reporting time?", required=(D.CASE_STUDIES,)
    )
    assert result.assessment.decision is K.STALE
    assert "REQUIRED_DOMAIN_MISSING:CASE_STUDIES" in result.assessment.deterministic_flags


# ---- Structured-fact conflicts -----------------------------------------------------------


def pricing(source_id: str, value: str, **overrides: object) -> str:
    return yaml_doc(
        meta(source_id=source_id, domain="PRICING_COMMERCIAL", title=f"Price list {source_id}", **overrides),
        [fact("plan.basic.monthly_price", value, "EUR", f"The Basic plan costs {value} EUR per month.")],
    )


def test_conflicting_structured_facts(db: Database, kb_root: Path) -> None:
    write(kb_root, "PRICING_COMMERCIAL", "a.yaml", pricing("prices-a", "100"))
    write(kb_root, "PRICING_COMMERCIAL", "b.yaml", pricing("prices-b", "120"))
    ingest(db, kb_root)
    assessment = evaluate(db, "What does the Basic plan cost per month?").assessment
    assert assessment.decision is K.CONFLICTING
    assert "FACT_CONFLICT:plan.basic.monthly_price" in assessment.deterministic_flags


def test_equal_facts_do_not_conflict(db: Database, kb_root: Path) -> None:
    write(kb_root, "PRICING_COMMERCIAL", "a.yaml", pricing("prices-a", "100"))
    write(kb_root, "PRICING_COMMERCIAL", "b.yaml", pricing("prices-b", "100"))
    ingest(db, kb_root)
    assert evaluate(db, "What does the Basic plan cost per month?").assessment.decision is K.SUFFICIENT


def test_unusable_sources_cannot_create_conflicts(db: Database, kb_root: Path) -> None:
    write(kb_root, "PRICING_COMMERCIAL", "a.yaml", pricing("prices-a", "100"))
    write(
        kb_root, "PRICING_COMMERCIAL", "b.yaml",
        pricing("prices-b", "120", approval_status="DRAFT", approved_by=None, approved_at=None),
    )
    ingest(db, kb_root)
    assert evaluate(db, "What does the Basic plan cost per month?").assessment.decision is K.SUFFICIENT


def test_superseded_version_facts_do_not_conflict(db: Database, kb_root: Path) -> None:
    write(kb_root, "PRICING_COMMERCIAL", "v1.yaml", pricing("prices", "100"))
    write(kb_root, "PRICING_COMMERCIAL", "v2.yaml", pricing("prices", "120", version=2))
    ingest(db, kb_root)
    result = evaluate(db, "What does the Basic plan cost per month?")
    assert result.assessment.decision is K.SUFFICIENT
    assert "120 EUR" in result.evidence[0].excerpt


# ---- Pure gate + invariants ---------------------------------------------------------------


def test_diagnostics_carry_no_text() -> None:
    assert not {"excerpt", "text", "body"} & set(DiagnosticHit.model_fields)


def test_pure_gate_uses_only_its_inputs() -> None:
    q = query("What is the refund window?")
    hits = [
        DiagnosticHit(
            question="What is the refund window?", source_id="s", source_version=1,
            domain=D.FAQ, usability=SourceUsability.STALE, coverage=1.0,
        )
    ]
    assert assess(q, [], diagnostics=hits).decision is K.STALE
    weak = [hit.model_copy(update={"coverage": 0.3}) for hit in hits]
    assert assess(q, [], diagnostics=weak).decision is K.INSUFFICIENT
    assert "NO_USABLE_EVIDENCE" in assess(q, []).deterministic_flags


def test_overall_decision_is_never_better_than_any_question(fixture_db: Database) -> None:
    assessment = evaluate(fixture_db, "What does the Basic plan cost per month?", PARTIAL_QUESTION).assessment
    assert [q.decision for q in assessment.per_question] == [K.SUFFICIENT, K.PARTIAL]
    assert assessment.decision is K.PARTIAL
    floor = combine_knowledge_decisions(q.decision for q in assessment.per_question)
    assert knowledge_severity(assessment.decision) >= knowledge_severity(floor)


def test_future_llm_opinion_can_only_downgrade(fixture_db: Database) -> None:
    assessment = evaluate(fixture_db, PARTIAL_QUESTION).assessment
    assert assessment.decision is K.PARTIAL
    data = assessment.model_dump()
    with pytest.raises(ValidationError, match="LLM opinion"):
        # A worse LLM opinion cannot sit beside a better deterministic decision...
        KnowledgeAssessment.model_validate(data | {"llm_sufficiency_opinion": K.INSUFFICIENT})
    # ...while a more optimistic opinion never upgrades anything.
    KnowledgeAssessment.model_validate(data | {"llm_sufficiency_opinion": K.SUFFICIENT})
    downgraded = data | {"decision": K.INSUFFICIENT, "llm_sufficiency_opinion": K.INSUFFICIENT}
    assert KnowledgeAssessment.model_validate(downgraded).decision is K.INSUFFICIENT
