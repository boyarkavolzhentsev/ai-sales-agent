from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, NonNegativeInt, StrictStr, StringConstraints, model_validator

from app.core.enums import KnowledgeDomain
from app.core.models import KnowledgeAssessment, KnowledgeEvidence, KnowledgeSource
from app.core.models.base import CoreModel
from app.core.models.types import EntityId, NonEmptyStr, Version
from app.core.validation import require_non_blank, unique_items

# Dotted lower-case fact key, e.g. "plan.basic.monthly_price".
FactKey = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_.-]*$", max_length=128)]
StrictText = Annotated[StrictStr, StringConstraints(min_length=1)]


class Fact(CoreModel):
    """One structured fact. Values are strings on purpose: they are compared verbatim
    (after case-folding) for conflict detection, never parsed or rounded."""

    key: FactKey
    value: StrictText
    unit: StrictText | None = None
    statement: StrictText | None = None

    @model_validator(mode="after")
    def _check_text(self) -> Self:
        for text in (self.value, self.unit, self.statement):
            if text is not None:
                require_non_blank(text)
        return self


class LoadedSource(CoreModel):
    """A parsed, validated source file ready for chunking."""

    source: KnowledgeSource
    body: str
    facts: tuple[Fact, ...] = ()

    @model_validator(mode="after")
    def _check_content(self) -> Self:
        if not self.body.strip() and not self.facts:
            raise ValueError("source has no content: body is empty and there are no facts")
        unique_items(tuple(fact.key for fact in self.facts))
        return self


class SourceUsability(StrEnum):
    """Why a source version can or cannot back a customer-facing answer."""

    USABLE = "USABLE"
    NOT_APPROVED = "NOT_APPROVED"  # DRAFT or RETIRED
    INTERNAL_ONLY = "INTERNAL_ONLY"
    WITHDRAWN = "WITHDRAWN"  # the newest version of this source_id is RETIRED
    NOT_YET_EFFECTIVE = "NOT_YET_EFFECTIVE"
    STALE = "STALE"  # past review_by
    LOCALE_MISMATCH = "LOCALE_MISMATCH"
    SUPERSEDED = "SUPERSEDED"  # a newer usable version, or another usable source, replaces it


class DiagnosticHit(CoreModel):
    """Evidence that an *unusable* source would have covered a question.

    Carries no text on purpose: diagnostics explain an escalation reason and can never
    be turned back into evidence for an answer.
    """

    question: NonEmptyStr
    source_id: EntityId
    source_version: Version
    domain: KnowledgeDomain
    usability: SourceUsability
    coverage: Annotated[float, Field(ge=0.0, le=1.0)]


class IngestStatus(StrEnum):
    INGESTED = "INGESTED"
    UNCHANGED = "UNCHANGED"  # identical source_id + version + content already present


class IngestResult(CoreModel):
    status: IngestStatus
    source: KnowledgeSource
    chunk_count: NonNegativeInt


class RetrievalMethod(StrEnum):
    LEXICAL = "LEXICAL"  # FTS5 candidates, BM25 over the eligible corpus (Stage 4)
    SEMANTIC = "SEMANTIC"  # cosine similarity of stored chunk vectors (Stage 19)


class RetrievalInfo(CoreModel):
    """How the evidence was selected: identifiers and counts only, never text or vectors."""

    method: RetrievalMethod
    provider: StrictText | None = None
    model: StrictText | None = None
    dimensions: Annotated[int, Field(ge=1)] | None = None
    min_similarity: Annotated[float, Field(ge=0.0, le=1.0)] | None = None
    eligible_chunks: NonNegativeInt = 0
    searchable_chunks: NonNegativeInt = 0


class KnowledgeResult(CoreModel):
    """Retrieved evidence and the deterministic gate verdict for one query."""

    evidence: tuple[KnowledgeEvidence, ...]
    assessment: KnowledgeAssessment
    retrieval: RetrievalInfo = RetrievalInfo(method=RetrievalMethod.LEXICAL)
