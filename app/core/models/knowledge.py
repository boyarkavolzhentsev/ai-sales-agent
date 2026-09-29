"""Knowledge contracts. No ingestion, retrieval or embeddings are implemented here."""

from typing import Annotated, Self

from pydantic import AfterValidator, AwareDatetime, Field, NonNegativeInt, PositiveInt, model_validator

from app.core.decisions.knowledge import combine_knowledge_decisions, knowledge_severity
from app.core.enums import (
    KnowledgeApprovalStatus,
    KnowledgeDecision,
    KnowledgeDomain,
    KnowledgeExternalUse,
    KnowledgePurpose,
)
from app.core.models.base import CoreModel
from app.core.models.types import (
    EntityId,
    LocaleTag,
    NonEmptyStr,
    Sha256Hex,
    UniqueEntityIds,
    UniqueNonEmptyStrs,
    Version,
)
from app.core.validation import ensure_after, unique_items

UniqueDomains = Annotated[tuple[KnowledgeDomain, ...], AfterValidator(unique_items)]


class KnowledgeSource(CoreModel):
    """One knowledge document at one version. Immutable per version."""

    source_id: EntityId
    domain: KnowledgeDomain
    title: NonEmptyStr
    path: NonEmptyStr
    version: Version
    content_hash: Sha256Hex
    approval_status: KnowledgeApprovalStatus
    external_use: KnowledgeExternalUse
    approved_by: NonEmptyStr | None = None
    approved_at: AwareDatetime | None = None
    effective_from: AwareDatetime | None = None
    review_by: AwareDatetime | None = None
    supersedes: EntityId | None = None
    locale: LocaleTag
    tags: UniqueNonEmptyStrs = ()

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        if (self.approved_by is None) != (self.approved_at is None):
            raise ValueError("approved_by and approved_at must be set together")
        if self.approval_status is KnowledgeApprovalStatus.APPROVED and self.approved_by is None:
            raise ValueError("an APPROVED source requires approved_by and approved_at")
        if self.approval_status is KnowledgeApprovalStatus.DRAFT and self.approved_by is not None:
            raise ValueError("a DRAFT source must not carry approval fields")
        if self.supersedes == self.source_id:
            raise ValueError("a source cannot supersede itself")
        ensure_after(self.review_by, self.effective_from, "review_by", "effective_from")
        return self


class KnowledgeChunk(CoreModel):
    """A retrievable unit of a knowledge source. Derived and rebuildable."""

    chunk_id: EntityId
    source_id: EntityId
    source_version: Version
    ordinal: NonNegativeInt
    text: NonEmptyStr
    content_hash: Sha256Hex
    embedding_ref: NonEmptyStr | None = None


class KnowledgeQuery(CoreModel):
    """One retrieval request. ``required_domains`` must be a subset of ``allowed_domains``."""

    query_id: EntityId
    purpose: KnowledgePurpose
    questions: Annotated[UniqueNonEmptyStrs, Field(min_length=1)]
    allowed_domains: Annotated[UniqueDomains, Field(min_length=1)]
    required_domains: UniqueDomains = ()
    locale: LocaleTag
    top_k: PositiveInt
    correlation_id: EntityId

    @model_validator(mode="after")
    def _check_domains(self) -> Self:
        outside = [domain for domain in self.required_domains if domain not in self.allowed_domains]
        if outside:
            raise ValueError(f"required_domains not in allowed_domains: {outside}")
        return self


class KnowledgeEvidence(CoreModel):
    """Snapshot of one retrieved chunk exactly as it was used. Immutable."""

    evidence_id: EntityId
    query_id: EntityId
    chunk_id: EntityId
    source_id: EntityId
    source_version: Version
    domain: KnowledgeDomain
    excerpt: NonEmptyStr
    score: float  # finite: enforced by CoreModel (allow_inf_nan=False)
    rank: PositiveInt
    approval_status: KnowledgeApprovalStatus
    external_use: KnowledgeExternalUse
    review_by: AwareDatetime | None = None


class QuestionAssessment(CoreModel):
    question: NonEmptyStr
    decision: KnowledgeDecision
    evidence_ids: UniqueEntityIds = ()


class KnowledgeAssessment(CoreModel):
    """The knowledge gate's verdict for one query.

    The overall ``decision`` may never be better than the worst per-question decision
    (no per-question entries count as INSUFFICIENT), nor better than the LLM's
    sufficiency opinion: the LLM may only downgrade.
    """

    query_id: EntityId
    decision: KnowledgeDecision
    per_question: tuple[QuestionAssessment, ...] = ()
    deterministic_flags: UniqueNonEmptyStrs = ()
    llm_sufficiency_opinion: KnowledgeDecision | None = None
    reasons: UniqueNonEmptyStrs = ()

    @model_validator(mode="after")
    def _check_decision(self) -> Self:
        unique_items(tuple(item.question for item in self.per_question))
        floor = combine_knowledge_decisions(item.decision for item in self.per_question)
        if knowledge_severity(self.decision) < knowledge_severity(floor):
            raise ValueError(f"decision {self.decision} is better than per-question result {floor}")
        opinion = self.llm_sufficiency_opinion
        if opinion is not None and knowledge_severity(self.decision) < knowledge_severity(opinion):
            raise ValueError(f"decision {self.decision} is better than the LLM opinion {opinion}")
        return self
