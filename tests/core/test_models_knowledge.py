from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.core.enums import (
    KnowledgeApprovalStatus,
    KnowledgeDecision,
    KnowledgeDomain,
    KnowledgeExternalUse,
    KnowledgePurpose,
)
from app.core.models import (
    KnowledgeAssessment,
    KnowledgeChunk,
    KnowledgeEvidence,
    KnowledgeQuery,
    KnowledgeSource,
    QuestionAssessment,
)

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
NAIVE = datetime(2026, 1, 1, 12, 0)
HASH = "c" * 64


def source_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "source_id": "src-1",
        "domain": KnowledgeDomain.PRICING_COMMERCIAL,
        "title": "Price list",
        "path": "knowledge_base/pricing/price_list.yaml",
        "version": 1,
        "content_hash": HASH,
        "approval_status": KnowledgeApprovalStatus.APPROVED,
        "external_use": KnowledgeExternalUse.EXTERNAL_OK,
        "approved_by": "head-of-sales",
        "approved_at": T0,
        "effective_from": T0,
        "review_by": T0 + timedelta(days=90),
        "locale": "en",
    }
    return base | overrides


def query_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "query_id": "q-1",
        "purpose": KnowledgePurpose.INBOUND_REPLY,
        "questions": ("What does the basic plan cost?",),
        "allowed_domains": (KnowledgeDomain.PRICING_COMMERCIAL, KnowledgeDomain.FAQ),
        "required_domains": (KnowledgeDomain.PRICING_COMMERCIAL,),
        "locale": "en",
        "top_k": 5,
        "correlation_id": "corr-1",
    }
    return base | overrides


def evidence_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "evidence_id": "ev-1",
        "query_id": "q-1",
        "chunk_id": "chunk-1",
        "source_id": "src-1",
        "source_version": 1,
        "domain": KnowledgeDomain.PRICING_COMMERCIAL,
        "excerpt": "Basic plan: 100 EUR / month",
        "score": 0.82,
        "rank": 1,
        "approval_status": KnowledgeApprovalStatus.APPROVED,
        "external_use": KnowledgeExternalUse.EXTERNAL_OK,
    }
    return base | overrides


# ---- KnowledgeSource --------------------------------------------------------


def test_approved_external_ok_source_is_valid() -> None:
    source = KnowledgeSource(**source_kwargs())
    assert source.approval_status is KnowledgeApprovalStatus.APPROVED
    assert source.external_use is KnowledgeExternalUse.EXTERNAL_OK


def test_approved_source_requires_approval_fields() -> None:
    with pytest.raises(ValidationError, match="APPROVED"):
        KnowledgeSource(**source_kwargs(approved_by=None, approved_at=None))
    with pytest.raises(ValidationError, match="together"):
        KnowledgeSource(**source_kwargs(approved_at=None))


def test_draft_source_must_not_carry_approval() -> None:
    with pytest.raises(ValidationError, match="DRAFT"):
        KnowledgeSource(**source_kwargs(approval_status=KnowledgeApprovalStatus.DRAFT))
    KnowledgeSource(
        **source_kwargs(
            approval_status=KnowledgeApprovalStatus.DRAFT, approved_by=None, approved_at=None
        )
    )


def test_internal_only_and_retired_sources_are_representable() -> None:
    KnowledgeSource(**source_kwargs(external_use=KnowledgeExternalUse.INTERNAL_ONLY))
    KnowledgeSource(**source_kwargs(approval_status=KnowledgeApprovalStatus.RETIRED))


def test_source_dates_supersedes_and_tags() -> None:
    with pytest.raises(ValidationError, match="review_by"):
        KnowledgeSource(**source_kwargs(review_by=T0))
    with pytest.raises(ValidationError, match="supersede"):
        KnowledgeSource(**source_kwargs(supersedes="src-1"))
    with pytest.raises(ValidationError, match="duplicate"):
        KnowledgeSource(**source_kwargs(tags=("pricing", "pricing")))
    with pytest.raises(ValidationError):
        KnowledgeSource(**source_kwargs(approved_at=NAIVE))
    with pytest.raises(ValidationError):
        KnowledgeSource(**source_kwargs(version=0))


def test_chunk() -> None:
    KnowledgeChunk(
        chunk_id="chunk-1", source_id="src-1", source_version=1, ordinal=0, text="x", content_hash=HASH
    )
    with pytest.raises(ValidationError):
        KnowledgeChunk(
            chunk_id="chunk-1", source_id="src-1", source_version=1, ordinal=-1, text="x", content_hash=HASH
        )
    with pytest.raises(ValidationError):
        KnowledgeChunk(
            chunk_id="chunk-1", source_id="src-1", source_version=1, ordinal=0, text=" ", content_hash=HASH
        )


# ---- KnowledgeQuery ---------------------------------------------------------


def test_query_is_valid() -> None:
    assert KnowledgeQuery(**query_kwargs()).top_k == 5


@pytest.mark.parametrize("top_k", [0, -1])
def test_top_k_must_be_positive(top_k: int) -> None:
    with pytest.raises(ValidationError):
        KnowledgeQuery(**query_kwargs(top_k=top_k))


def test_query_questions_and_domains() -> None:
    with pytest.raises(ValidationError):
        KnowledgeQuery(**query_kwargs(questions=()))
    with pytest.raises(ValidationError, match="duplicate"):
        KnowledgeQuery(**query_kwargs(questions=("a", "a")))
    with pytest.raises(ValidationError):
        KnowledgeQuery(**query_kwargs(allowed_domains=(), required_domains=()))
    with pytest.raises(ValidationError, match="required_domains"):
        KnowledgeQuery(**query_kwargs(required_domains=(KnowledgeDomain.LEGAL_COMPLIANCE,)))


# ---- KnowledgeEvidence ------------------------------------------------------


def test_evidence_is_valid() -> None:
    assert KnowledgeEvidence(**evidence_kwargs()).rank == 1


@pytest.mark.parametrize("rank", [0, -3])
def test_evidence_rank_must_be_at_least_one(rank: int) -> None:
    with pytest.raises(ValidationError):
        KnowledgeEvidence(**evidence_kwargs(rank=rank))


@pytest.mark.parametrize("score", [float("nan"), float("inf"), float("-inf")])
def test_evidence_score_must_be_finite(score: float) -> None:
    with pytest.raises(ValidationError):
        KnowledgeEvidence(**evidence_kwargs(score=score))


# ---- KnowledgeAssessment ----------------------------------------------------


def _qa(question: str, decision: KnowledgeDecision) -> QuestionAssessment:
    return QuestionAssessment(question=question, decision=decision, evidence_ids=("ev-1",))


def test_assessment_matching_worst_question_is_valid() -> None:
    assessment = KnowledgeAssessment(
        query_id="q-1",
        decision=KnowledgeDecision.PARTIAL,
        per_question=(
            _qa("a", KnowledgeDecision.SUFFICIENT),
            _qa("b", KnowledgeDecision.PARTIAL),
        ),
    )
    assert assessment.decision is KnowledgeDecision.PARTIAL


def test_assessment_cannot_be_better_than_per_question_result() -> None:
    with pytest.raises(ValidationError, match="better than per-question"):
        KnowledgeAssessment(
            query_id="q-1",
            decision=KnowledgeDecision.SUFFICIENT,
            per_question=(_qa("a", KnowledgeDecision.STALE),),
        )


def test_assessment_without_questions_cannot_claim_sufficiency() -> None:
    with pytest.raises(ValidationError, match="better than per-question"):
        KnowledgeAssessment(query_id="q-1", decision=KnowledgeDecision.SUFFICIENT)
    KnowledgeAssessment(query_id="q-1", decision=KnowledgeDecision.INSUFFICIENT)


def test_assessment_may_be_worse_than_per_question_result() -> None:
    KnowledgeAssessment(
        query_id="q-1",
        decision=KnowledgeDecision.CONFLICTING,
        per_question=(_qa("a", KnowledgeDecision.SUFFICIENT),),
        deterministic_flags=("PRICE_CONFLICT",),
    )


def test_llm_opinion_can_only_downgrade() -> None:
    with pytest.raises(ValidationError, match="LLM opinion"):
        KnowledgeAssessment(
            query_id="q-1",
            decision=KnowledgeDecision.SUFFICIENT,
            per_question=(_qa("a", KnowledgeDecision.SUFFICIENT),),
            llm_sufficiency_opinion=KnowledgeDecision.PARTIAL,
        )
    KnowledgeAssessment(
        query_id="q-1",
        decision=KnowledgeDecision.PARTIAL,
        per_question=(_qa("a", KnowledgeDecision.SUFFICIENT),),
        llm_sufficiency_opinion=KnowledgeDecision.PARTIAL,
    )


def test_assessment_questions_unique() -> None:
    with pytest.raises(ValidationError, match="duplicate"):
        KnowledgeAssessment(
            query_id="q-1",
            decision=KnowledgeDecision.SUFFICIENT,
            per_question=(
                _qa("a", KnowledgeDecision.SUFFICIENT),
                _qa("a", KnowledgeDecision.SUFFICIENT),
            ),
        )
